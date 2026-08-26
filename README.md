# TriLayer Imóveis — MVP

Descrições de imóveis PT-PT (e EN) para agentes imobiliários, geradas por IA.
Produto de teste da **TriLayer Engineering** (trilayer.dev).

## Arquitetura

- **Front-end:** `index.html` estático → GitHub Pages (grátis)
- **API:** `api/main.py` (FastAPI) → Render free tier
- **LLM:** Gemini Flash (`gemini-2.5-flash`) via API key
- **Pagamentos:** Stripe Checkout (pagamentos únicos, sem subscrições)

## Modelo de negócio — passes com validade

Compra um passe de acesso por um período; o acesso é ativado de imediato e **não há reembolsos**
(consentimento explícito no checkout, como exige a lei da UE para serviços digitais).

| Passe | Preço (IVA incl.) | Validade | Limite |
|---|---|---|---|
| day | €1,00 | 24 h | 20 descrições/dia |
| week | €4,99 | 7 dias | 100 descrições/semana |
| month | €9,99 | 30 dias | ilimitado |

- Compra em cima de compra **acumula** (`valid_until = max(valid_until, agora) + duração`)
- O tipo de passe sobe sempre para o mais forte ativo (dia → semana → mês)
- Conta `ADMIN_USER` tem bypass (não precisa de passe — é do dono)
- Demo anónima: 2 descrições/dia por dispositivo (+ teto por IP)
- Registo com **email** (sem verificação por agora) — o email da conta é o destinatário do recibo do Stripe
- **Histórico**: cada geração fica guardada na conta (máx. 50) — "As minhas descrições" na página
- **Modo refinar**: cola um anúncio existente e a IA reescreve em PT-PT (mantém factos, corrige pt-BR, mesma saída) — conta 1 geração
- **Fotografias (visão)**: anexa até 3 fotos ao formulário e a IA usa o que vê (divisões, piscina, varanda, estado…) para detalhar a descrição — os dados do formulário têm prioridade; não inventa o que não vê
- **Lembretes de expiração**: cron diário envia email 72h antes do passe expirar (Resend)

## Deploy

### 1. GitHub Pages
```bash
cd ~/projects/trilayer-imoveis
git add -A && git commit -m "feat: ..."
git push origin main
```
GitHub → repo → Settings → Pages → Source: `main` / root →
`https://gui816.github.io/trilayer-imoveis/`

### 2. Render (API)
- New → Web Service → ligar o mesmo repo
- Root directory: `api`
- Build: `pip install -r requirements.txt`
- Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Env vars (ver `api/render.yaml`):
  - `GEMINI_API_KEY` — obrigatória
  - `ADMIN_USER` / `ADMIN_PASSWORD` — conta inicial (do dono, com bypass)
  - `STRIPE_SECRET_KEY` — `sk_test_...` (teste) ou `sk_live_...`
  - `STRIPE_WEBHOOK_SECRET` — `whsec_...` do webhook (ver passo 3)
  - `SITE_URL` — URL do GitHub Pages (para o Stripe voltar após pagamento)
  - `BACKOFFICE_PASSWORD` — acesso ao back-office (`https://<api>/backoffice`)
  - `DEMO_LIMIT_PER_DAY` (2), `DEMO_IP_LIMIT_PER_DAY` (10), `MAX_ACTIVE_SESSIONS` (1), `TZ` (Europe/Lisbon)
- Depois, no `index.html`: `const API_URL = "https://<teu-serviço>.onrender.com"` e faz push.

### 3. Stripe
1. Conta em stripe.com/pt (ativar em modo teste)
2. Dashboard → Developers → API keys → copiar `sk_test_...`
3. Dashboard → Developers → Webhooks → **Add endpoint**:
   - URL: `https://<teu-serviço>.onrender.com/webhook/stripe`
   - Eventos: `checkout.session.completed`
   - Copiar o **Signing secret** (`whsec_...`) → `STRIPE_WEBHOOK_SECRET`
4. Testar: comprar um passe no site com o cartão de teste `4242 4242 4242 4242`
5. Para produção: ativar a conta Stripe, trocar para `sk_live_...` + webhook live

> Nota dev: em local sem webhook público, podes testar com
> `ALLOW_UNSIGNED_WEBHOOK=1` (aceita eventos sem assinatura — nunca em produção).

### 4. Back-office (super admin)
`https://<api>.onrender.com/backoffice` — acesso com **email + password** da conta autorizada:
- `SUPER_ADMIN_EMAIL` — email da conta que acede (se vazio, usa a conta `ADMIN_USER`)

Painel completo: receita total + últimos 6 meses, passes ativos, contas (criação, passe,
validade, usos hoje, gerações totais, gasto total), compras com nº de fatura e botão **PDF**
(fatura-recibo automática), atividade recente (gerações), passes a expirar em 3 dias,
estado do webhook, e **exportar CSV** das faturas para a comunicação à AT.

Faturação — preencher no Render:
- `FATURA_NOME` — o teu nome (ou nome comercial)
- `FATURA_NIF` — o teu NIF (sem isto o PDF devolve 503)
- `FATURA_MORADA` — morada fiscal
- `FATURA_CAE` — default `62090`
- `FATURA_ISENTO` — `1` (default) → sem IVA, referência art. 53.º CIVA; `0` → IVA 23% destacado

Numeracão sequencial automática por ano (`FT 2026/0001`…) e estável (mesma compra → mesmo número).
> Nota: a comunicação à AT (app gratuita do Portal das Finanças) é manual — para volume de MVP,
> 5 min/mês. Quando justificar, migrar para software certificado (ex: InvoiceXpress) com comunicação automática.

### 6. Lembretes de expiração (emails)
- Criar conta em **resend.com** (grátis, 3.000 emails/mês) → API key → `RESEND_API_KEY` no Render
- `RESEND_FROM` — remetente (default `TriLayer Imóveis <onboarding@resend.dev>`; com domínio próprio, ex: `avisos@casaquevende.pt`)
- `CRON_SECRET` — segredo para o cron diário
- Endpoint: `POST /api/cron/expiring-reminders` com header `X-Cron-Secret` — envia email aos clientes com passe a expirar em 72h (uma vez por validade)
- Cron local (OpenClaw, 09:00 Lisboa): lê `~/.openclaw/workspace/trilayer-cron.env` e chama o endpoint — job `trilayer-expiry-reminders`

## Testes
```bash
cd api && ../.venv/bin/python -m uvicorn main:app --reload
# testes de fluxo: ver histórico do projeto (TestClient com webhook unsigned)
```

## Custos

| Item | Custo |
|---|---|
| Gemini Flash por descrição (~1.5k in + 1.2k out) | ~€0,0006 |
| 20 descrições/dia | ~€0,01-0,02/dia |
| GitHub Pages + Render free + Stripe (sem tarifa fixa) | €0 |
| Stripe por transação | 1,4% + €0,25 |

→ €1/dia (≈€30/mês) cobre os custos ~50-100x. Mesmo com retries/erros a dobrarem,
a margem é enorme. O €9,99/mês ilimitado também é lucro desde o 1.º cliente.
