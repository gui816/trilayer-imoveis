"""
TriLayer Imóveis — API
Gera descrições de imóveis PT-PT (e EN) via Gemini.

Deploy: Render (free) — uvicorn main:app
Env vars:
  GEMINI_API_KEY       (obrigatório)
  ADMIN_USER           (default "gui")      — conta inicial criada no primeiro boot
  ADMIN_PASSWORD       (default "trilayer") — password da conta inicial
  LIMIT_PER_DAY        (default 20)
  LIMIT_PER_WEEK       (default 50)
  LIMIT_PER_MONTH      (default 200)
  MAX_ACTIVE_SESSIONS  (default 1) — 1 = unicidade de uso: novo login revoga as sessões
                                     anteriores, por isso a conta não pode ser partilhada
  DEMO_LIMIT_PER_DAY   (default 1)  — descrições demo grátis por dia e por dispositivo
  DEMO_IP_LIMIT_PER_DAY (default 2)  — teto de segurança por IP (protege contra novo device_id)
  GEMINI_MODEL         (default gemini-2.5-flash)

Contas: users.json (hash PBKDF2-SHA256 das passwords e dos tokens; nunca plaintext).
Uso: contadores por conta (dia/semana/mês), não globais.
"""

import os
import json
import time
import hashlib
import secrets
import threading
from datetime import date

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
LIMIT_PER_DAY = int(os.environ.get("LIMIT_PER_DAY", "20"))
LIMIT_PER_WEEK = int(os.environ.get("LIMIT_PER_WEEK", "50"))
LIMIT_PER_MONTH = int(os.environ.get("LIMIT_PER_MONTH", "200"))
MAX_ACTIVE_SESSIONS = int(os.environ.get("MAX_ACTIVE_SESSIONS", "1"))
DEMO_LIMIT_PER_DAY = int(os.environ.get("DEMO_LIMIT_PER_DAY", "1"))
DEMO_IP_LIMIT_PER_DAY = int(os.environ.get("DEMO_IP_LIMIT_PER_DAY", "2"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.json")
IP_USAGE_FILE = os.path.join(BASE_DIR, "ip_usage.json")

_lock = threading.Lock()

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


class GenerateRequest(BaseModel):
    token: str = ""
    device_id: str = ""
    dados: DadosImovel


# ── Helpers de segurança ─────────────────────────────────
def _hash_secret(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 100_000).hex()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        }
        _save_users(users)


def _fresh_usage() -> dict:
    today = date.today()
    iso = today.isocalendar()
    return {
        "day": today.isoformat(),
        "day_count": 0,
        "week": f"{iso[0]}-W{iso[1]:02d}",
        "week_count": 0,
        "month": today.strftime("%Y-%m"),
        "month_count": 0,
    }


def _roll_usage(usage: dict) -> dict:
    """Repõe os contadores cujo período já mudou."""
    today = date.today()
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
    if data.get("day") != date.today().isoformat():
        return 0
    return data.get("ips", {}).get(ip, 0)


def _bump_ip(ip: str) -> None:
    try:
        with open(IP_USAGE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    today = date.today().isoformat()
    if data.get("day") != today:
        data = {"day": today, "ips": {}}
    data["ips"][ip] = data["ips"].get(ip, 0) + 1
    with open(IP_USAGE_FILE, "w") as f:
        json.dump(data, f)


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


@app.get("/api/health")
def health():
    return {"ok": True}


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
        usage = _roll_usage(acc.get("usage") or _fresh_usage())
        acc["usage"] = usage
        _save_users(users)

    return {
        "token": token,
        "user": user,
        "limite": LIMIT_PER_DAY,
        "limite_semana": LIMIT_PER_WEEK,
        "limite_mes": LIMIT_PER_MONTH,
        "usos_hoje": usage["day_count"],
        "usos_semana": usage["week_count"],
        "usos_mes": usage["month_count"],
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
    token_hex = _token_hash(req.token)
    with _lock:
        users = _load_users()
        acc = None
        for a in users.values():
            for s in a.get("sessions", []):
                if s["token_hash"] == token_hex:
                    acc = a
                    break
            if acc:
                break
        if not acc:
            raise HTTPException(401, "Sessão expirada ou conta em uso noutro dispositivo. Inicia sessão novamente.")
        s["last_seen"] = time.time()
        usage = _roll_usage(acc.get("usage") or _fresh_usage())
        acc["usage"] = usage

        if usage["day_count"] >= LIMIT_PER_DAY:
            raise HTTPException(402, f"Limite diário atingido ({LIMIT_PER_DAY} descrições). Volta amanhã ou assina o plano ilimitado.")
        if usage["week_count"] >= LIMIT_PER_WEEK:
            raise HTTPException(402, f"Limite semanal atingido ({LIMIT_PER_WEEK} descrições). Volta na próxima semana ou assina o plano ilimitado.")
        if usage["month_count"] >= LIMIT_PER_MONTH:
            raise HTTPException(402, f"Limite mensal atingido ({LIMIT_PER_MONTH} descrições). Volta no próximo mês ou assina o plano ilimitado.")

        raw = call_gemini(build_prompt(req.dados))
        result = parse_json(raw)

        usage["day_count"] += 1
        usage["week_count"] += 1
        usage["month_count"] += 1
        _save_users(users)

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
        "usos_hoje": usage["day_count"],
        "limite": LIMIT_PER_DAY,
        "usos_semana": usage["week_count"],
        "limite_semana": LIMIT_PER_WEEK,
        "usos_mes": usage["month_count"],
        "limite_mes": LIMIT_PER_MONTH,
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
