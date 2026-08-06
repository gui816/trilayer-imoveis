# TriLayer Imóveis — MVP

Descrições de imóveis PT-PT (e EN) para agentes imobiliários, geradas por IA.
Produto de teste da **TriLayer Engineering** (trilayer.dev).

## Arquitetura

- **Front-end:** `index.html` estático → GitHub Pages (grátis)
- **API:** `api/main.py` (FastAPI) → Render free tier (onde já tens o CobraAi/meal-scanner)
- **LLM:** Gemini Flash (`gemini-2.5-flash`) via API key

## Deploy

### 1. GitHub Pages
```bash
cd ~/projects/trilayer-imoveis
git init && git add -A && git commit -m "feat: trilayer imoveis MVP"
# criar repo no GitHub (ex: trilayer-imoveis) e:
git remote add origin git@github.com:gui816/trilayer-imoveis.git
git push -u origin main
```
GitHub → repo → Settings → Pages → Source: `main` / root → online em
`https://gui816.github.io/trilayer-imoveis/`

### 2. Render (API)
- New → Web Service → ligar o mesmo repo
- Root directory: `api`
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env vars:
  - `GEMINI_API_KEY` (tens no `.env.local` do cobraai)
  - `API_PASSWORD` = a password de teste (ex: algo forte)
  - `LIMIT_PER_DAY` = `20`
- Depois, no `index.html`: `const API_URL = "https://<teu-serviço>.onrender.com"` e volta a fazer push.

### 3. Testar
1. Abre o GitHub Pages → password → formulário
2. Clica "Preencher exemplo" → "Gerar descrições"
3. Verifica se a descrição é PT-PT (não pt-BR) — esse é o diferencial

## Monetização (planeada, não implementada no MVP)

- **€1 / dia** → máximo 20 descrições (já está o limite no código — devolve 402)
- **€9,99 / mês** → ilimitado (falta: pagamentos Stripe + flag de subscrito)
- O contador de usos está em `api/usage.json` (reset diário automático)

## Custos (por que €1/dia cobre tudo)

| Item | Custo |
|---|---|
| Gemini Flash por descrição (~1.5k in + 1.2k out) | ~€0,0006 |
| 20 descrições/dia | ~€0,01-0,02/dia |
| GitHub Pages + Render free | €0 |
| **Total mensal (600 descrições)** | **~€0,40-0,60** |

→ €1/dia (≈€30/mês) cobre os custos **~50-100x**. Mesmo com retries/erros a dobrarem,
a margem é enorme. O €9,99/mês ilimitado também é lucro desde o 1.º cliente.
