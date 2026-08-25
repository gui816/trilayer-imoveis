"""
TriLayer Imóveis — API
Gera descrições de imóveis PT-PT (e EN) via Gemini.

Deploy: Render (free) — uvicorn main:app
Env vars:
  GEMINI_API_KEY  (obrigatório)
  API_PASSWORD    (password de acesso de teste)
  LIMIT_PER_DAY   (default 20)
  LIMIT_PER_WEEK  (default 50)
  LIMIT_PER_MONTH (default 200)
  GEMINI_MODEL    (default gemini-2.5-flash)
"""

import os
import json
import time
from datetime import date

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="TriLayer Imóveis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # GitHub Pages; restringir quando sair do teste
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

API_PASSWORD = os.environ.get("API_PASSWORD", "troca-esta-password")
LIMIT_PER_DAY = int(os.environ.get("LIMIT_PER_DAY", "20"))
LIMIT_PER_WEEK = int(os.environ.get("LIMIT_PER_WEEK", "50"))
LIMIT_PER_MONTH = int(os.environ.get("LIMIT_PER_MONTH", "200"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
USAGE_FILE = os.path.join(os.path.dirname(__file__), "usage.json")

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
  "titulo_seo": "T2 reabilitado com garagem e varanda em Alvalade — 295.000 €",
  "descricao_curta": "máx. 300 caracteres, 1 parágrafo",
  "descricao_longa": "600-900 caracteres, 3-4 parágrafos, PT-PT",
  "descricao_en": "versão inglesa da longa",
  "perguntas_respostas": [
    {"pergunta": "Qual é a morada exata?", "resposta": "Resposta modelo de 1-2 frases (o agente personaliza depois)."},
    {"pergunta": "Tem estacionamento/garagem?", "resposta": "..."},
    {"pergunta": "Quais são as despesas de condomínio?", "resposta": "..."},
    {"pergunta": "Quando é possível visitar?", "resposta": "..."}
  ]
}"""


class DadosImovel(BaseModel):
    tipologia: str = ""
    preco: str = ""
    zona: str = ""
    area: str = ""
    andar: str = ""
    estado: str = ""
    caracteristicas: str = ""
    publico: str = ""


class GenerateRequest(BaseModel):
    password: str
    dados: DadosImovel


def _week_key(d):
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def read_usage() -> dict:
    try:
        with open(USAGE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    today = date.today()
    day, week, month = today.isoformat(), _week_key(today), today.strftime("%Y-%m")
    if data.get("day") != day:
        data["day"], data["day_count"] = day, 0
    if data.get("week") != week:
        data["week"], data["week_count"] = week, 0
    if data.get("month") != month:
        data["month"], data["month_count"] = month, 0
    return data


def write_usage(data: dict) -> None:
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)


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
    # remove fences de markdown se existirem
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:
        # fallback: extrair o primeiro objeto JSON
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
    return f"{SYSTEM_PROMPT}\n\nDADOS DO IMÓVEL:\n{linha}\nCaracterísticas: {carac}\n{pub}"


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/generate")
def generate(req: GenerateRequest):
    if req.password != API_PASSWORD:
        raise HTTPException(401, "Password incorreta.")

    usage = read_usage()
    if usage["day_count"] >= LIMIT_PER_DAY:
        raise HTTPException(402, f"Limite diário atingido ({LIMIT_PER_DAY} descrições). Volta amanhã ou assina o plano ilimitado.")
    if usage["week_count"] >= LIMIT_PER_WEEK:
        raise HTTPException(402, f"Limite semanal atingido ({LIMIT_PER_WEEK} descrições). Volta na próxima semana ou assina o plano ilimitado.")
    if usage["month_count"] >= LIMIT_PER_MONTH:
        raise HTTPException(402, f"Limite mensal atingido ({LIMIT_PER_MONTH} descrições). Volta no próximo mês ou assina o plano ilimitado.")

    t0 = time.time()
    raw = call_gemini(build_prompt(req.dados))
    result = parse_json(raw)

    usage["day_count"] += 1
    usage["week_count"] += 1
    usage["month_count"] += 1
    write_usage(usage)

    return {
        "titulo_seo": result.get("titulo_seo", ""),
        "descricao_curta": result.get("descricao_curta", ""),
        "descricao_longa": result.get("descricao_longa", ""),
        "descricao_en": result.get("descricao_en", ""),
        "perguntas_respostas": result.get("perguntas_respostas", []),
        "usos_hoje": usage["day_count"],
        "limite": LIMIT_PER_DAY,
        "usos_semana": usage["week_count"],
        "limite_semana": LIMIT_PER_WEEK,
        "usos_mes": usage["month_count"],
        "limite_mes": LIMIT_PER_MONTH,
        "segundos": round(time.time() - t0, 1),
    }
