"""
TriLayer Imóveis — API
Gera descrições de imóveis PT-PT (e EN) via Gemini.

Deploy: Render (free) — uvicorn main:app
Env vars:
  GEMINI_API_KEY        (obrigatório)
  ADMIN_USER            (default "gui")      — conta inicial criada no primeiro boot
  ADMIN_PASSWORD        (default "trilayer") — password da conta inicial
  STRIPE_SECRET_KEY     (default "") — chave secreta Stripe (sk_test_... / sk_live_...)
  STRIPE_WEBHOOK_SECRET (default "") — assinatura do webhook (Dashboard → Webhooks)
  SITE_URL              (default https://gui816.github.io/trilayer-imoveis/) — para success/cancel do Checkout
  BACKOFFICE_PASSWORD   (default = ADMIN_PASSWORD) — acesso ao /backoffice
  MAX_ACTIVE_SESSIONS   (default 1) — 1 = unicidade de uso: novo login revoga as sessões
                                      anteriores, por isso a conta não pode ser partilhada
  DEMO_LIMIT_PER_DAY    (default 2)  — descrições demo grátis por dia e por dispositivo
  DEMO_IP_LIMIT_PER_DAY (default 10) — teto de segurança por IP (protege contra novo device_id)
  GEMINI_MODEL          (default gemini-2.5-flash)
  TZ                    (default Europe/Lisbon)
  ALLOW_UNSIGNED_WEBHOOK (default "0") — "1" permite webhook sem assinatura (SÓ para testes locais)

Modelo de negócio — PASSES COM VALIDADE (pagamento único, sem subscrições):
  day   €1,00  → 24h, máx. 20 descrições por dia
  week  €4,99  → 7 dias, máx. 100 descrições por semana
  month €9,99  → 30 dias, ilimitado
  Compra em cima de compra acumula (valid_until = max(valid_until, agora) + duração)
  e o tipo de passe sobe para o mais forte ativo. Sem reembolsos (consentimento no checkout).

Contas: users.json (hash PBKDF2-SHA256 das passwords e dos tokens; nunca plaintext).
Uso: contadores por conta (dia/semana/mês), não globais.
"""

import os
import re
import json
import time
import hashlib
import secrets
import threading
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import requests
import stripe
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

app = FastAPI(title="TriLayer Imóveis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # GitHub Pages; restringir quando sair do teste
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ── Config ───────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "gui")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "trilayer")
BACKOFFICE_PASSWORD = os.environ.get("BACKOFFICE_PASSWORD", ADMIN_PASSWORD)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ACTIVE_SESSIONS = int(os.environ.get("MAX_ACTIVE_SESSIONS", "1"))
DEMO_LIMIT_PER_DAY = int(os.environ.get("DEMO_LIMIT_PER_DAY", "2"))
DEMO_IP_LIMIT_PER_DAY = int(os.environ.get("DEMO_IP_LIMIT_PER_DAY", "10"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SITE_URL = os.environ.get("SITE_URL", "https://gui816.github.io/trilayer-imoveis/")
ALLOW_UNSIGNED_WEBHOOK = os.environ.get("ALLOW_UNSIGNED_WEBHOOK", "0") == "1"
LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Lisbon"))

# ── Faturação (fatura-recibo, ENI) ──────────────────────
# Preenche FATURA_NIF para ativar; FATURA_ISENTO=1 (default) → art. 53.º CIVA (sem IVA).
FATURA_NOME = os.environ.get("FATURA_NOME", "TriLayer Engineering")
FATURA_NIF = os.environ.get("FATURA_NIF", "")
FATURA_MORADA = os.environ.get("FATURA_MORADA", "")
FATURA_CAE = os.environ.get("FATURA_CAE", "62090")
FATURA_ISENTO = os.environ.get("FATURA_ISENTO", "1") == "1"
PLAN_ITEMS = {
    "day": "Passe diário — descrições de imóveis com IA (24 horas)",
    "week": "Passe semanal — descrições de imóveis com IA (7 dias)",
    "month": "Passe mensal — descrições de imóveis com IA (30 dias)",
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
IP_USAGE_FILE = os.path.join(BASE_DIR, "ip_usage.json")

_lock = threading.Lock()
_bo_tokens = {}  # token de back-office → expira (em memória; reinicia com o processo)

# ── Planos (passes com validade) ─────────────────────────
# cap: (período_do_contador, máximo) — ("day", 20) limita o contador diário;
# (None, None) = sem limite.
PLANS = {
    "day": {"label": "Diário", "amount_cents": 100, "duration_s": 24 * 3600, "cap": ("day", 20)},
    "week": {"label": "Semanal", "amount_cents": 499, "duration_s": 7 * 24 * 3600, "cap": ("week", 100)},
    "month": {"label": "Mensal", "amount_cents": 999, "duration_s": 30 * 24 * 3600, "cap": (None, None)},
}
PASS_RANK = {"day": 1, "week": 2, "month": 3}

SYSTEM_PROMPT = """És um copywriter especialista em imobiliário português (Portugal, pt-PT).

REGRAS OBRIGATÓRIAS:
- Escreve SEMPRE em português de Portugal (pt-PT). NUNCA uses pt-BR. Proibido: "apartamento de 2 quartos", "armários", "varanda gourmet", "cozinha americana" (diz "cozinha em open space"), "andar alto", "condomínio fechado" (diz "condomínio privado").
- Vocabulário PT-PT de imobiliário: fração, arrecadação, escritura, caderneta predial, certificado energético, área bruta/útil, tipologia T0-T5, "prédio com elevador", "3.º andar", "visto gold", "obras de conservação", "licença de utilização".
- Tom: profissional, caloroso, sem exageros de agência ("oportunidade única" no máximo 1x). Nada de "sonho", "magnífico" repetido.
- Factos só do que foi fornecido. Se faltar info (ex: não há área), não inventes — omite ou generaliza.
- Preço: escreve como "295.000 €".
- A descrição longa deve destacar a zona (qualidade de vida, transportes, serviços) de forma sóbria e vender os pontos fortes.
- A descrição EN é tradução natural para inglês (UK), não literal, com o mesmo tom.

RESPONDE APENAS COM JSON válido, sem markdown, com esta estrutura exata:
{
  "titulos_seo": ["Título SEO 1", "Título SEO 2", "Título SEO 3"],
  "titulos_en": ["English SEO title 1", "English SEO title 2", "English SEO title 3"],
  "ganchos": ["Primeira frase de abertura 1", "Abertura 2", "Abertura 3"],
  "ganchos_en": ["English opening 1", "English opening 2", "English opening 3"],
  "descricao_curta": "máx. 300 caracteres, 1 parágrafo",
  "descricao_longa": "600-900 caracteres, 3-4 parágrafos, PT-PT",
  "descricao_en": "versão inglesa da longa",
  "perguntas_respostas": [
    {"pergunta": "Qual é a morada exata?", "resposta": "Resposta modelo de 1-2 frases (o agente personaliza depois)."},
    {"pergunta": "Tem estacionamento/garagem?", "resposta": "..."},
    {"pergunta": "Quais são as despesas de condomínio?", "resposta": "..."},
    {"pergunta": "Quando é possível visitar?", "resposta": "..."}
  ]
}

REGRAS DOS 3 TÍTULOS E 3 ABERTURAS:
- Os 3 títulos SEO devem ter ângulos diferentes: um focado na localização, outro nas características/chave de venda, outro no estilo de vida ou preço. Cada um máx. 60 caracteres.
- Os 3 ganchos (primeira frase da descrição longa) devem ter tons diferentes: um sóbrio/profissional, um emocional/envolvente, um técnico/detalhado. Cada um máx. 160 caracteres.
- `titulos_en` e `ganchos_en` são traduções naturais para inglês (UK) dos correspondentes PT — mesmo ângulo e tom, nunca tradução literal.
- A descrição longa deve começar com o gancho escolhido de forma natural — usa o gancho 1 como abertura padrão da descrição longa.
"""


class DadosImovel(BaseModel):
    tipologia: str = ""
    preco: str = ""
    zona: str = ""
    area: str = ""
    andar: str = ""
    estado: str = ""
    caracteristicas: str = ""
    publico: str = ""
    extra: str = ""


class LoginRequest(BaseModel):
    user: str
    password: str
    device_id: str = ""


class RegisterRequest(BaseModel):
    user: str
    password: str
    device_id: str = ""


class GenerateRequest(BaseModel):
    token: str = ""
    device_id: str = ""
    dados: DadosImovel


class CheckoutRequest(BaseModel):
    token: str
    plan: str
    consent: bool = False


class BackofficeLogin(BaseModel):
    password: str


# ── Helpers de segurança ─────────────────────────────────
def _hash_secret(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 100_000).hex()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso(epoch: float) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, LOCAL_TZ).strftime("%d/%m/%Y %H:%M")


# ── Persistência de contas ───────────────────────────────
def _load_users() -> dict:
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_users(data: dict) -> None:
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _ensure_admin() -> None:
    """Cria a conta admin no primeiro boot se ainda não existir."""
    with _lock:
        users = _load_users()
        if ADMIN_USER in users:
            return
        salt = _new_salt()
        users[ADMIN_USER] = {
            "password_salt": salt,
            "password_hash": _hash_secret(ADMIN_PASSWORD, salt),
            "sessions": [],
            "usage": _fresh_usage(),
            "pass": None,
            "purchases": [],
            "created": time.time(),
        }
        _save_users(users)


def _fresh_usage() -> dict:
    today = _today()
    iso = today.isocalendar()
    return {
        "day": today.isoformat(),
        "day_count": 0,
        "week": f"{iso[0]}-W{iso[1]:02d}",
        "week_count": 0,
        "month": today.strftime("%Y-%m"),
        "month_count": 0,
    }


def _today():
    return datetime.now(LOCAL_TZ).date()


def _roll_usage(usage: dict) -> dict:
    """Repõe os contadores cujo período já mudou."""
    today = _today()
    iso = today.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    month = today.strftime("%Y-%m")
    if usage.get("day") != today.isoformat():
        usage["day"], usage["day_count"] = today.isoformat(), 0
    if usage.get("week") != week:
        usage["week"], usage["week_count"] = week, 0
    if usage.get("month") != month:
        usage["month"], usage["month_count"] = month, 0
    return usage


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _ip_count(ip: str) -> int:
    try:
        with open(IP_USAGE_FILE) as f:
            data = json.load(f)
    except Exception:
        return 0
    if data.get("day") != _today().isoformat():
        return 0
    return data.get("ips", {}).get(ip, 0)


def _bump_ip(ip: str) -> None:
    try:
        with open(IP_USAGE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    today = _today().isoformat()
    if data.get("day") != today:
        data = {"day": today, "ips": {}}
    data["ips"][ip] = data["ips"].get(ip, 0) + 1
    with open(IP_USAGE_FILE, "w") as f:
        json.dump(data, f)


def _find_by_token(users: dict, token: str):
    """Devolve (user_key, account, session) para um token de sessão, ou (None, None, None)."""
    token_hex = _token_hash(token)
    for k, a in users.items():
        for s in a.get("sessions", []):
            if s["token_hash"] == token_hex:
                return k, a, s
    return None, None, None


def _active_pass(acc: dict, now: float):
    """Devolve (pass_type, valid_until) se houver passe ativo, senão (None, 0)."""
    pt = acc.get("pass") or {}
    valid_until = pt.get("valid_until", 0) or 0
    if valid_until > now:
        return pt.get("type"), valid_until
    return None, 0


# ── Stripe ───────────────────────────────────────────────
def _ensure_stripe_prices() -> None:
    """Cria o produto e os 3 preços no Stripe uma única vez (idempotente via metadata)."""
    if not STRIPE_SECRET_KEY:
        return
    stripe.api_key = STRIPE_SECRET_KEY
    product = None
    for p in stripe.Product.list(limit=100).data:
        if p.metadata.get("tl_product") == "1":
            product = p
            break
    if not product:
        product = stripe.Product.create(name="TriLayer Imóveis — Passes", metadata={"tl_product": "1"})
    for plan, cfg in PLANS.items():
        found = None
        for pr in stripe.Price.list(product=product.id, limit=100).data:
            if pr.metadata.get("tl_plan") == plan:
                found = pr
                break
        if not found:
            found = stripe.Price.create(
                unit_amount=cfg["amount_cents"],
                currency="eur",
                product=product.id,
                metadata={"tl_plan": plan},
            )
        cfg["price_id"] = found.id


# ── Gemini ───────────────────────────────────────────────
def call_gemini(prompt: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise HTTPException(500, "GEMINI_API_KEY não configurada no servidor.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192},
    }
    r = requests.post(url, params={"key": key}, json=body, timeout=90)
    if r.status_code != 200:
        raise HTTPException(502, f"Gemini falhou ({r.status_code}): {r.text[:200]}")
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise HTTPException(502, "Resposta da Gemini sem conteúdo.")


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise HTTPException(502, "A Gemini não devolveu JSON válido.")


def build_prompt(d: DadosImovel) -> str:
    linha = (
        f"Imóvel: {d.tipologia} em {d.zona}"
        + (f", {d.area} m² de área bruta" if d.area else "")
        + (f", {d.andar}" if d.andar else "")
        + (f", {d.estado}" if d.estado else "")
        + (f", preço {d.preco} €" if d.preco else "")
    )
    carac = d.caracteristicas or "sem características adicionais fornecidas"
    pub = f"Público-alvo: {d.publico}." if d.publico else ""
    extra = f"Informações adicionais: {d.extra}." if d.extra else ""
    return f"{SYSTEM_PROMPT}\n\nDADOS DO IMÓVEL:\n{linha}\nCaracterísticas: {carac}\n{pub}\n{extra}"


# ── Endpoints ────────────────────────────────────────────
@app.on_event("startup")
def _startup():
    _ensure_admin()
    _ensure_stripe_prices()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/register")
def register(req: RegisterRequest):
    """Cria uma conta com email (necessária para comprar passes) e devolve sessão iniciada."""
    user = req.user.strip().lower()
    if len(user) > 254 or not EMAIL_RE.match(user):
        raise HTTPException(400, "Introduz um email válido (ex: agente@imobiliaria.pt).")
    if len(req.password) < 8:
        raise HTTPException(400, "A password tem de ter pelo menos 8 caracteres.")
    with _lock:
        users = _load_users()
        if user in users:
            raise HTTPException(409, "Esse utilizador já existe. Inicia sessão.")
        salt = _new_salt()
        token = secrets.token_urlsafe(32)
        token_hex = _token_hash(token)
        users[user] = {
            "password_salt": salt,
            "password_hash": _hash_secret(req.password, salt),
            "sessions": [{"token_hash": token_hex, "device_id": req.device_id, "created": time.time(), "last_seen": time.time()}],
            "usage": _fresh_usage(),
            "pass": None,
            "purchases": [],
            "created": time.time(),
        }
        _save_users(users)
    return {"token": token, "user": user}


@app.post("/api/login")
def login(req: LoginRequest):
    user = req.user.strip().lower()
    with _lock:
        users = _load_users()
        acc = users.get(user)
        if not acc:
            raise HTTPException(401, "Utilizador ou password incorretos.")
        if _hash_secret(req.password, acc["password_salt"]) != acc["password_hash"]:
            raise HTTPException(401, "Utilizador ou password incorretos.")

        token = secrets.token_urlsafe(32)
        token_hex = _token_hash(token)
        now = time.time()
        session = {"token_hash": token_hex, "device_id": req.device_id, "created": now, "last_seen": now}

        # Unicidade de uso: mantém apenas as MAX_ACTIVE_SESSIONS mais recentes.
        # Com default 1, qualquer novo login revoga as sessões anteriores — a conta
        # não pode ser usada em dois dispositivos ao mesmo tempo (nem partilhada).
        acc["sessions"] = acc["sessions"] or []
        acc["sessions"].append(session)
        acc["sessions"] = sorted(acc["sessions"], key=lambda s: s["last_seen"], reverse=True)[:MAX_ACTIVE_SESSIONS]
        acc["usage"] = _roll_usage(acc.get("usage") or _fresh_usage())
        _save_users(users)

    return {"token": token, "user": user}


@app.post("/api/checkout")
def checkout(req: CheckoutRequest):
    """Cria uma sessão de Checkout Stripe para comprar um passe (pagamento único)."""
    plan = req.plan.strip().lower()
    if plan not in PLANS:
        raise HTTPException(400, "Plano inválido. Escolhe day, week ou month.")
    if not req.consent:
        raise HTTPException(400, "É necessário aceitar a política de sem reembolsos para continuar.")
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Pagamentos ainda não configurados no servidor (falta STRIPE_SECRET_KEY).")

    with _lock:
        users = _load_users()
        user_key, acc, _ = _find_by_token(users, req.token)
        if not acc:
            raise HTTPException(401, "Sessão expirada ou conta em uso noutro dispositivo. Inicia sessão novamente.")
        if user_key == ADMIN_USER:
            # O admin não compra passes — tem acesso sempre (bypass no /api/generate).
            raise HTTPException(400, "A conta de administrador não precisa de passes.")

        stripe.api_key = STRIPE_SECRET_KEY
        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{"price": PLANS[plan]["price_id"], "quantity": 1}],
                success_url=SITE_URL + "#comprado",
                cancel_url=SITE_URL,
                customer_email=user_key,  # recibo do Stripe vai para o email da conta
                metadata={"user": user_key, "plan": plan, "consent": "true"},
            )
        except Exception as e:
            raise HTTPException(502, f"Falha ao criar o pagamento no Stripe: {e}")

        acc["pending"] = {"session_id": session.get("id"), "plan": plan, "created": time.time()}
        _save_users(users)

    return {"url": session["url"], "plan": plan}


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    """Recebe eventos do Stripe. O único evento tratado: checkout.session.completed."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        except Exception:
            raise HTTPException(400, "Assinatura do webhook inválida.")
    elif ALLOW_UNSIGNED_WEBHOOK:
        event = json.loads(payload)  # modo dev: sem validação (nunca em produção)
    else:
        raise HTTPException(503, "Webhook não configurado (falta STRIPE_WEBHOOK_SECRET).")

    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "ignored": event.get("type")}

    sess = event["data"]["object"]
    metadata = sess.get("metadata") or {}
    user_key = metadata.get("user", "")
    plan = metadata.get("plan", "")
    sid = sess.get("id", "")
    if not user_key or plan not in PLANS:
        return {"ok": True, "skipped": "missing_metadata"}

    with _lock:
        users = _load_users()
        acc = users.get(user_key)
        if not acc:
            return {"ok": True, "skipped": "unknown_user"}

        # Idempotência: o Stripe pode reenviar o mesmo evento — nunca ativa duas vezes.
        for p in acc.get("purchases", []):
            if p.get("session_id") == sid:
                return {"ok": True, "skipped": "duplicate"}

        now = time.time()
        current_type, current_until = _active_pass(acc, now)
        base = max(current_until, now)
        new_type = plan if PASS_RANK[plan] >= PASS_RANK.get(current_type, 0) else current_type
        acc["pass"] = {"type": new_type, "valid_until": base + PLANS[plan]["duration_s"]}
        acc.setdefault("purchases", []).append({
            "session_id": sid,
            "plan": plan,
            "amount_cents": sess.get("amount_total", PLANS[plan]["amount_cents"]),
            "created": now,
            "consent": True,
            "email": user_key,
        })
        acc.pop("pending", None)
        _save_users(users)

    return {"ok": True, "activated": user_key, "plan": plan}


@app.get("/api/plan")
def plan_status(token: str = ""):
    """Estado do passe e do uso da conta (para a barra de plano no front)."""
    if not token:
        raise HTTPException(400, "Falta o token.")
    with _lock:
        users = _load_users()
        user_key, acc, _ = _find_by_token(users, token)
        if not acc:
            raise HTTPException(401, "Sessão inválida.")
        usage = _roll_usage(acc.get("usage") or _fresh_usage())
        acc["usage"] = usage
        _save_users(users)
        pass_type, valid_until = _active_pass(acc, time.time())
        purchases = sorted(acc.get("purchases", []), key=lambda p: p.get("created", 0), reverse=True)[:10]

    caps = PLANS[pass_type]["cap"] if pass_type else (None, None)
    return {
        "active": bool(pass_type),
        "pass_type": pass_type,
        "plan_label": PLANS[pass_type]["label"] if pass_type else None,
        "valid_until": _iso(valid_until) if pass_type else None,
        "usage": {"hoje": usage["day_count"], "semana": usage["week_count"], "mes": usage["month_count"]},
        "caps": {"dia": caps[1] if caps[0] == "day" else None, "semana": caps[1] if caps[0] == "week" else None},
        "is_admin": user_key == ADMIN_USER,
        "purchases": [{"plan": p["plan"], "amount_cents": p.get("amount_cents", 0), "created": _iso(p.get("created", 0))} for p in purchases],
    }


@app.post("/api/generate")
def generate(req: GenerateRequest, request: Request):
    t0 = time.time()

    # ── Modo demo (anónimo): sem token, identificado por device_id ──
    if not req.token:
        if not req.device_id:
            raise HTTPException(400, "Falta o identificador do dispositivo (device_id).")
        ip = _client_ip(request)
        key = "demo:" + req.device_id
        with _lock:
            users = _load_users()
            acc = users.get(key) or {
                "password_salt": "",
                "password_hash": "",
                "sessions": [],
                "usage": _fresh_usage(),
            }
            usage = _roll_usage(acc["usage"])
            if usage["day_count"] >= DEMO_LIMIT_PER_DAY:
                _un = "descrição" if DEMO_LIMIT_PER_DAY == 1 else "descrições"
                raise HTTPException(
                    402,
                    f"Demo grátis esgotada ({DEMO_LIMIT_PER_DAY} {_un} por dia e por dispositivo). Cria uma conta para continuar.",
                )
            if _ip_count(ip) >= DEMO_IP_LIMIT_PER_DAY:
                raise HTTPException(
                    429,
                    f"Limite de demo por rede atingido ({DEMO_IP_LIMIT_PER_DAY} descrições por dia a partir deste IP). Cria uma conta para continuar.",
                )

            raw = call_gemini(build_prompt(req.dados))
            result = parse_json(raw)

            usage["day_count"] += 1
            acc["usage"] = usage
            users[key] = acc
            _save_users(users)
            _bump_ip(ip)

        return {
            "titulos_seo": result.get("titulos_seo", []),
            "titulos_en": result.get("titulos_en", []),
            "ganchos": result.get("ganchos", []),
            "ganchos_en": result.get("ganchos_en", []),
            "descricao_curta": result.get("descricao_curta", ""),
            "descricao_longa": result.get("descricao_longa", ""),
            "descricao_en": result.get("descricao_en", ""),
            "perguntas_respostas": result.get("perguntas_respostas", []),
            "demo": True,
            "usos_hoje": usage["day_count"],
            "limite": DEMO_LIMIT_PER_DAY,
            "segundos": round(time.time() - t0, 1),
        }

    # ── Modo conta: token de sessão ──
    with _lock:
        users = _load_users()
        user_key, acc, sess = _find_by_token(users, req.token)
        if not acc:
            raise HTTPException(401, "Sessão expirada ou conta em uso noutro dispositivo. Inicia sessão novamente.")
        sess["last_seen"] = time.time()
        usage = _roll_usage(acc.get("usage") or _fresh_usage())
        acc["usage"] = usage

        now = time.time()
        is_admin = user_key == ADMIN_USER
        pass_type, valid_until = (None, 0) if is_admin else _active_pass(acc, now)

        if not is_admin:
            if not pass_type:
                raise HTTPException(402, "Sem passe ativo. Compra 1 dia, 1 semana ou 1 mês para gerar descrições.")
            per, cap = PLANS[pass_type]["cap"]
            if per == "day" and usage["day_count"] >= cap:
                raise HTTPException(402, f"Limite do passe diário atingido ({cap} descrições hoje). Compra outro passe para continuar.")
            if per == "week" and usage["week_count"] >= cap:
                raise HTTPException(402, f"Limite do passe semanal atingido ({cap} descrições esta semana). Compra outro passe para continuar.")

        raw = call_gemini(build_prompt(req.dados))
        result = parse_json(raw)

        usage["day_count"] += 1
        usage["week_count"] += 1
        usage["month_count"] += 1
        _save_users(users)

    if is_admin or pass_type == "month":
        lim_d = lim_w = lim_m = None
    elif pass_type == "day":
        lim_d, lim_w, lim_m = PLANS["day"]["cap"][1], None, None
    else:  # week
        lim_d, lim_w, lim_m = None, PLANS["week"]["cap"][1], None

    return {
        "titulos_seo": result.get("titulos_seo", []),
        "titulos_en": result.get("titulos_en", []),
        "ganchos": result.get("ganchos", []),
        "ganchos_en": result.get("ganchos_en", []),
        "descricao_curta": result.get("descricao_curta", ""),
        "descricao_longa": result.get("descricao_longa", ""),
        "descricao_en": result.get("descricao_en", ""),
        "perguntas_respostas": result.get("perguntas_respostas", []),
        "demo": False,
        "pass_type": pass_type,
        "valid_until": _iso(valid_until) if pass_type else None,
        "usos_hoje": usage["day_count"],
        "limite": lim_d,
        "usos_semana": usage["week_count"],
        "limite_semana": lim_w,
        "usos_mes": usage["month_count"],
        "limite_mes": lim_m,
        "segundos": round(time.time() - t0, 1),
    }


@app.get("/api/demo")
def demo_status(device_id: str = ""):
    """Devolve o estado da demo para um dispositivo (para o aviso no front)."""
    if not device_id:
        return {"demo": True, "usos_hoje": 0, "limite": DEMO_LIMIT_PER_DAY}
    key = "demo:" + device_id
    users = _load_users()
    acc = users.get(key)
    usage = _roll_usage(acc["usage"]) if acc else _fresh_usage()
    return {"demo": True, "usos_hoje": usage["day_count"], "limite": DEMO_LIMIT_PER_DAY}


# ── Back-office ──────────────────────────────────────────
@app.post("/api/backoffice/login")
def backoffice_login(req: BackofficeLogin):
    if req.password != BACKOFFICE_PASSWORD:
        raise HTTPException(401, "Password incorreta.")
    tok = secrets.token_urlsafe(24)
    _bo_tokens[tok] = time.time() + 12 * 3600
    return {"token": tok}


@app.get("/api/backoffice/data")
def backoffice_data(x_token: str = Header(default="")):
    if _bo_tokens.get(x_token, 0) < time.time():
        raise HTTPException(401, "Não autorizado.")
    users = _load_users()
    rows, purchases = [], []
    revenue_cents = 0
    active_passes = 0
    demo_users = 0
    now = time.time()
    for k, a in users.items():
        if k.startswith("demo:"):
            demo_users += 1
            continue
        pass_type, valid_until = _active_pass(a, now)
        if pass_type:
            active_passes += 1
        rows.append({
            "user": k,
            "created": _iso(a.get("created", 0)),
            "pass_type": pass_type,
            "valid_until": _iso(valid_until) if pass_type else None,
            "usos_hoje": (a.get("usage") or {}).get("day_count", 0),
        })
        for p in a.get("purchases", []):
            revenue_cents += p.get("amount_cents", 0)
            purchases.append({
                "user": k,
                "plan": p.get("plan", ""),
                "amount_cents": p.get("amount_cents", 0),
                "created": _iso(p.get("created", 0)),
                "session": p.get("session_id", ""),
            })
    purchases.sort(key=lambda x: x["created"] or "", reverse=True)
    return {
        "totals": {
            "receita_cents": revenue_cents,
            "passes_ativos": active_passes,
            "contas": len(rows),
            "demo_dispositivos": demo_users,
        },
        "users": sorted(rows, key=lambda r: r["user"]),
        "purchases": purchases[:200],
    }


@app.get("/backoffice")
def backoffice_page():
    return FileResponse(os.path.join(BASE_DIR, "backoffice.html"))


# ── Faturação (PDF) ─────────────────────────────────────
def _fmt_eur(cents: int) -> str:
    return f"{cents / 100:.2f}".replace(".", ",") + " €"


def _assign_invoice_number(users: dict, purchase: dict, year: int) -> str:
    """Numeração sequencial estável por ano: FT 2026/0001, 0002..."""
    if purchase.get("num"):
        return purchase["num"]
    used = set()
    for a in users.values():
        for p in a.get("purchases", []):
            if p.get("num"):
                used.add(p["num"])
    i = 1
    while f"FT {year}/{i:04d}" in used:
        i += 1
    num = f"FT {year}/{i:04d}"
    purchase["num"] = num
    return num


def _invoice_pdf(purchase: dict, email: str) -> bytes:
    """Gera o PDF da fatura-recibo. Preços com IVA incluído; se FATURA_ISENTO,
    sem destaque de IVA (isenção art. 53.º CIVA)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    right = ParagraphStyle("right", fontSize=9, alignment=TA_RIGHT)
    title = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=16, leading=20, alignment=TA_RIGHT)
    h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=9, leading=12)
    n = ParagraphStyle("n", fontSize=9, leading=12)
    small = ParagraphStyle("small", fontSize=7.5, leading=10, textColor=colors.grey)

    created = datetime.fromtimestamp(purchase.get("created", time.time()), LOCAL_TZ)
    plan = purchase.get("plan", "")
    amount = purchase.get("amount_cents", 0)
    num = purchase.get("num", "")
    iva_pct = 0 if FATURA_ISENTO else 23
    base = round(amount / (1 + iva_pct / 100)) if iva_pct else amount
    iva = amount - base if iva_pct else 0

    elements = [
        Table(
            [[Paragraph("<b>" + FATURA_NOME + "</b>", n),
              Paragraph("FATURA-RECIBO", title)],
             [Paragraph(f"NIF: {FATURA_NIF}<br/>" + FATURA_MORADA + f"<br/>CAE: {FATURA_CAE}", small),
              Paragraph(f"<b>{num}</b><br/>{created.strftime('%d/%m/%Y %H:%M')}", right)]],
            colWidths=[90 * mm, 84 * mm],
        ),
        Spacer(1, 10 * mm),
        Paragraph("Cliente:", h),
        Paragraph(f"{email}<br/>Consumidor final (particular)", n),
        Spacer(1, 8 * mm),
        Table(
            [[Paragraph("Descrição", h), Paragraph("Qtd.", h), Paragraph("Preço unit.", h), Paragraph("Total", h)],
             [Paragraph(PLAN_ITEMS.get(plan, plan), n), Paragraph("1", n),
              Paragraph(_fmt_eur(amount), n), Paragraph(_fmt_eur(amount), n)]],
            colWidths=[110 * mm, 20 * mm, 22 * mm, 22 * mm],
            style=TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8d2c5")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2eee6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]),
        ),
        Spacer(1, 6 * mm),
        Table(
            [[Paragraph("Subtotal", n), Paragraph(_fmt_eur(base), right)],
             [Paragraph("IVA (" + str(iva_pct) + "%)" if iva_pct else "IVA — Isento (art. 53.º do CIVA)", n),
              Paragraph(_fmt_eur(iva) if iva_pct else "—", right)],
             [Paragraph("<b>TOTAL</b>", n), Paragraph("<b>" + _fmt_eur(amount) + "</b>", right)]],
            colWidths=[152 * mm, 42 * mm],
            style=TableStyle([("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#d8d2c5"))]),
        ),
        Spacer(1, 12 * mm),
        Paragraph("Pagamento recebido via Stripe (online). Documento gerado automaticamente — não carece de assinatura.", small),
    ]
    doc.build(elements)
    return buf.getvalue()


@app.get("/api/backoffice/invoice")
def backoffice_invoice(session_id: str = "", x_token: str = Header(default="")):
    """Devolve o PDF da fatura-recibo de uma compra (identificada pela session_id do Stripe)."""
    if _bo_tokens.get(x_token, 0) < time.time():
        raise HTTPException(401, "Não autorizado.")
    if not FATURA_NIF:
        raise HTTPException(503, "Faturação não configurada: define FATURA_NIF (e FATURA_NOME/FATURA_MORADA) no Render.")
    if not session_id:
        raise HTTPException(400, "Falta session_id.")

    with _lock:
        users = _load_users()
        for user_key, a in users.items():
            for p in a.get("purchases", []):
                if p.get("session_id") == session_id:
                    purchase = p
                    email = user_key
                    break
            else:
                continue
            break
        else:
            raise HTTPException(404, "Compra não encontrada.")
        year = datetime.fromtimestamp(purchase.get("created", time.time()), LOCAL_TZ).year
        num = _assign_invoice_number(users, purchase, year)
        _save_users(users)

    pdf = _invoice_pdf(purchase, email)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="fatura-recibo-{num.replace("/", "-")}.pdf"'})
