import os
import re
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler

try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8537829811:AAEMKGX_w3kRLPwEQFQ-QnUWO3BG9YmJECk")

# Diretório persistente: /home é volume montado e compartilhado entre Kudu e app container
DADOS_DIR = Path("/home/data")
DADOS_DIR.mkdir(parents=True, exist_ok=True)
DADOS_FILE = DADOS_DIR / "dados_praias.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_praias")

# Diagnostic: log onde os arquivos serão escritos
log.info(f"📁 DADOS_DIR = {DADOS_DIR} (exists={DADOS_DIR.exists()})")
log.info(f"📄 DADOS_FILE = {DADOS_FILE}")

# Todas as 27 praias monitoradas pela CPRH com seus municípios
PRAIAS_CONHECIDAS = [
    # Itamaracá
    {"praia": "Jaguaribe", "municipio": "Itamaracá"},
    {"praia": "Pilar", "municipio": "Itamaracá"},
    {"praia": "Forte Orange", "municipio": "Itamaracá"},
    # Igarassu
    {"praia": "Praia do Capitão (Mangue Seco)", "municipio": "Igarassu"},
    # Paulista
    {"praia": "Maria Farinha", "municipio": "Paulista"},
    {"praia": "Janga (Cond. Roberto Barbosa)", "municipio": "Paulista"},
    {"praia": "Janga (Rua Betânia)", "municipio": "Paulista"},
    # Olinda
    {"praia": "Rio Doce", "municipio": "Olinda"},
    {"praia": "Bairro Novo", "municipio": "Olinda"},
    {"praia": "Carmo", "municipio": "Olinda"},
    {"praia": "Milagres", "municipio": "Olinda"},
    # Recife
    {"praia": "Pina", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 8)", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 15)", "municipio": "Recife"},
    # Jaboatão dos Guararapes
    {"praia": "Piedade", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Candeias (Conj. Candeias II)", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Candeias (Rest. Candelária)", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Barra de Jangadas", "municipio": "Jaboatão dos Guararapes"},
    # Cabo de Santo Agostinho
    {"praia": "Suape", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Enseada dos Corais", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Gaibu", "municipio": "Cabo de Santo Agostinho"},
    # Ipojuca
    {"praia": "Porto de Galinhas", "municipio": "Ipojuca"},
    {"praia": "Ponta de Serrambi", "municipio": "Ipojuca"},
    # Tamandaré
    {"praia": "Praia dos Carneiros", "municipio": "Tamandaré"},
    {"praia": "Tamandaré (Hotel Marinas)", "municipio": "Tamandaré"},
    {"praia": "Tamandaré (Rua Nilo Gouveia)", "municipio": "Tamandaré"},
    # São José da Coroa Grande
    {"praia": "São José da Coroa Grande", "municipio": "São José da Coroa Grande"},
]


# ─── Mini HTTP server para Azure App Service F1 ──────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    """Responde a health checks do Azure na porta $PORT."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("🤖 Bot Praias PE — online".encode("utf-8"))

    def log_message(self, format, *args):
        pass  # silencia logs de health check

def iniciar_http_server():
    """Inicia HTTP server numa thread separada para manter o F1 acordado."""
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"🌐 HTTP health server na porta {port}")


def self_ping():
    """Faz ping no proprio endpoint para evitar que o Azure F1 durma."""
    url = os.environ.get("WEBSITE_HOSTNAME", "")
    if not url:
        return
    try:
        resp = requests.get(f"https://{url}/", timeout=10)
        log.info(f"Self-ping: {resp.status_code}")
    except Exception as e:
        log.warning(f"Self-ping falhou: {e}")


# ─── Scraper CPRH ─────────────────────────────────────────────────────────────

def parse_status(texto):
    t = texto.upper().strip()
    if "IMPR" in t:
        return "IMPRÓPRIA"
    if "PR" in t:
        return "PRÓPRIA"
    return None


def scrape_pdf(pdf_url):
    """Extrai dados de um PDF da CPRH."""
    praias = []
    try:
        log.info(f"Baixando PDF: {pdf_url}")
        resp = requests.get(pdf_url, timeout=30, verify=False)
        pdf_path = DADOS_DIR / "temp_balneabilidade.pdf"
        pdf_path.write_bytes(resp.content)

        status_atual = None
        municipio_atual = ""

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                texto = page.extract_text() or ""
                for linha in texto.splitlines():
                    linha = linha.strip()
                    if not linha:
                        continue

                    # Detecta linha de status
                    if re.match(r"^(PR[OÓ]PRIA|IMPR[OÓ]PRIA)$", linha, re.IGNORECASE):
                        status_atual = parse_status(linha)
                        continue

                    # Detecta município (linha curta sem números)
                    if (len(linha) < 35
                            and not any(c.isdigit() for c in linha)
                            and "praia" not in linha.lower()
                            and "em frente" not in linha.lower()
                            and linha not in ["PRÓPRIA", "IMPRÓPRIA"]):
                        municipio_atual = linha
                        continue

                    # Linha de praia
                    if ("praia" in linha.lower() or "em frente" in linha.lower()) and status_atual:
                        # Extrai nome limpo da praia
                        nome = re.sub(r"(?i)^praia\s+(de|da|do|dos|das)\s+", "", linha)
                        nome = nome.split(",")[0].split("–")[0].strip()
                        praias.append({
                            "praia": nome,
                            "status": status_atual,
                            "municipio": municipio_atual
                        })

        pdf_path.unlink(missing_ok=True)
        log.info(f"PDF extraído: {len(praias)} praias")

    except Exception as e:
        log.error(f"Erro ao processar PDF: {e}")

    return praias


def encontrar_pdf_cprh():
    """Busca o link do PDF mais recente no site da CPRH."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(
            "https://www2.cprh.pe.gov.br/monitoramento-ambiental/balneabilidade/",
            headers=headers, timeout=15, verify=False
        )
        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "balneabilidade" in href.lower() and href.lower().endswith(".pdf"):
                if href.startswith("http"):
                    return href
                return "https://www2.cprh.pe.gov.br/" + href.lstrip("/")

        # Tenta também na página de uploads do WordPress
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "uploads" in href and href.lower().endswith(".pdf"):
                return href if href.startswith("http") else "https://www2.cprh.pe.gov.br/" + href.lstrip("/")

    except Exception as e:
        log.error(f"Erro ao buscar PDF: {e}")

    return None


def atualizar_dados():
    """Busca dados da CPRH e atualiza o arquivo local."""
    log.info("🔄 Atualizando dados da CPRH...")

    if not PDF_SUPPORT:
        log.error("pdfplumber não instalado")
        return

    pdf_url = encontrar_pdf_cprh()
    praias = []

    if pdf_url:
        praias = scrape_pdf(pdf_url)
    else:
        log.warning("PDF não encontrado no site da CPRH")

    if praias:
        dados = {
            "atualizado_em": datetime.now().isoformat(),
            "total_proprias": sum(1 for p in praias if p["status"] == "PRÓPRIA"),
            "total_improprias": sum(1 for p in praias if p["status"] == "IMPRÓPRIA"),
            "praias": praias,
            "fonte": "cprh"
        }
        DADOS_FILE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"✅ {len(praias)} praias salvas da CPRH")
    else:
        log.warning("⚠️ Scraper não encontrou dados — mantendo dados anteriores")


# ─── Dados de exemplo (fallback com todas as 27 praias) ───────────────────────

def dados_exemplo():
    """Retorna dados de exemplo com todas as praias conhecidas."""
    # Boletim 18/2026 — Publicado 30/04/2026, vigência 30/04 a 07/05
    # 13 próprias / 14 impróprias
    # Mudanças vs Bol. 17:
    #   PIORARAM: Pilar, Janga (Cond. Roberto Barbosa), Candeias (Conj. Candeias II)
    #   MELHORARAM: Maria Farinha
    status_map = {
        "Jaguaribe": "IMPRÓPRIA",
        "Pilar": "IMPRÓPRIA",                            # ← piorou
        "Forte Orange": "PRÓPRIA",
        "Praia do Capitão (Mangue Seco)": "PRÓPRIA",
        "Maria Farinha": "PRÓPRIA",                      # ← melhorou
        "Janga (Cond. Roberto Barbosa)": "IMPRÓPRIA",    # ← piorou
        "Janga (Rua Betânia)": "IMPRÓPRIA",
        "Rio Doce": "IMPRÓPRIA",
        "Bairro Novo": "IMPRÓPRIA",
        "Carmo": "IMPRÓPRIA",
        "Milagres": "IMPRÓPRIA",
        "Pina": "IMPRÓPRIA",
        "Boa Viagem (Posto 8)": "PRÓPRIA",
        "Boa Viagem (Posto 15)": "PRÓPRIA",
        "Piedade": "PRÓPRIA",
        "Candeias (Conj. Candeias II)": "IMPRÓPRIA",     # ← piorou (5422)
        "Candeias (Rest. Candelária)": "IMPRÓPRIA",      # (6476)
        "Barra de Jangadas": "IMPRÓPRIA",
        "Suape": "IMPRÓPRIA",
        "Enseada dos Corais": "IMPRÓPRIA",
        "Gaibu": "PRÓPRIA",
        "Porto de Galinhas": "PRÓPRIA",
        "Ponta de Serrambi": "PRÓPRIA",
        "Praia dos Carneiros": "PRÓPRIA",
        "Tamandaré (Hotel Marinas)": "PRÓPRIA",
        "Tamandaré (Rua Nilo Gouveia)": "PRÓPRIA",
        "São José da Coroa Grande": "PRÓPRIA",
    }

    praias = []
    for p in PRAIAS_CONHECIDAS:
        praias.append({
            "praia": p["praia"],
            "municipio": p["municipio"],
            "status": status_map.get(p["praia"], "PRÓPRIA")
        })

    return {
        "atualizado_em": "2026-05-06T12:00:00",
        "boletim_nr": "18/2026",
        "periodo": "30/04 a 07/05",
        "publicado_em": "30/04/2026",
        "alteracoes_melhora": ["Maria Farinha"],
        "alteracoes_piora": ["Pilar", "Janga (Cond. Roberto Barbosa)", "Candeias (Conj. Candeias II)"],
        "total_proprias": sum(1 for p in praias if p["status"] == "PRÓPRIA"),
        "total_improprias": sum(1 for p in praias if p["status"] == "IMPRÓPRIA"),
        "praias": praias,
        "fonte": "exemplo"
    }


def carregar_dados():
    exemplo = dados_exemplo()
    if DADOS_FILE.exists():
        dados = json.loads(DADOS_FILE.read_text(encoding="utf-8"))
        # Se tem dados reais da CPRH e são mais recentes que o exemplo, usa
        if len(dados.get("praias", [])) >= 10:
            data_disco = dados.get("atualizado_em", "2000-01-01")
            data_exemplo = exemplo.get("atualizado_em", "2000-01-01")
            if data_disco >= data_exemplo:
                return dados
            # Exemplo é mais recente (boletim actualizado no código) — usa exemplo
            log.info(f"⚠️ Dados em disco ({data_disco}) mais antigos que exemplo ({data_exemplo}) — usando exemplo")
    DADOS_FILE.write_text(json.dumps(exemplo, ensure_ascii=False, indent=2), encoding="utf-8")
    return exemplo


# ─── Persistência de assinantes ───────────────────────────────────────────────

ASSINANTES_FILE = DADOS_DIR / "assinantes.json"
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # opcional, p/ avisos

def carregar_assinantes():
    if ASSINANTES_FILE.exists():
        try:
            return set(json.loads(ASSINANTES_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def salvar_assinantes(assinantes):
    ASSINANTES_FILE.write_text(
        json.dumps(sorted(assinantes), ensure_ascii=False),
        encoding="utf-8"
    )

def adicionar_assinante(chat_id):
    assinantes = carregar_assinantes()
    if chat_id not in assinantes:
        assinantes.add(chat_id)
        salvar_assinantes(assinantes)
        log.info(f"➕ Novo assinante: {chat_id} (total: {len(assinantes)})")


# ─── Detecção de boletim novo + broadcast ────────────────────────────────────

def assinatura_boletim(dados):
    """Cria assinatura única do boletim para detectar mudanças."""
    nr = dados.get("boletim_nr", "")
    # Concatena (praia, status) ordenado para detectar qualquer mudança
    sig = ";".join(
        f"{p.get('praia','')}={p.get('status','')}"
        for p in sorted(dados.get("praias", []), key=lambda x: x.get("praia", ""))
    )
    return f"{nr}|{sig}"

async def broadcast_boletim(application, dados_anterior=None):
    """Envia boletim atualizado para todos os assinantes."""
    assinantes = carregar_assinantes()
    if not assinantes:
        log.info("📭 Nenhum assinante para notificar")
        return

    texto = "🆕 BOLETIM ATUALIZADO!\n\n" + formatar_boletim()
    enviados = 0
    falhas = 0
    bloqueados = []

    for chat_id in assinantes:
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=texto,
                parse_mode="Markdown"
            )
            enviados += 1
        except Exception as e:
            falhas += 1
            erro = str(e).lower()
            # Remove usuários que bloquearam o bot
            if "blocked" in erro or "chat not found" in erro or "deactivated" in erro:
                bloqueados.append(chat_id)
            log.warning(f"Falha enviando para {chat_id}: {e}")

    # Limpa bloqueados
    if bloqueados:
        assinantes_atuais = carregar_assinantes()
        for cid in bloqueados:
            assinantes_atuais.discard(cid)
        salvar_assinantes(assinantes_atuais)
        log.info(f"🧹 Removidos {len(bloqueados)} chats inativos")

    log.info(f"📢 Broadcast: {enviados} enviados, {falhas} falhas")

async def verificar_e_notificar(application):
    """Compara boletim atual com último broadcast e notifica se mudou."""
    SIG_FILE = DADOS_DIR / "ultima_assinatura.txt"
    dados = carregar_dados()
    sig_atual = assinatura_boletim(dados)

    sig_anterior = ""
    if SIG_FILE.exists():
        sig_anterior = SIG_FILE.read_text(encoding="utf-8").strip()

    if sig_atual != sig_anterior:
        log.info(f"🆕 Boletim mudou! Enviando broadcast...")
        await broadcast_boletim(application)
        SIG_FILE.write_text(sig_atual, encoding="utf-8")
    else:
        log.info("⏭️ Boletim sem mudanças, sem broadcast")


# ─── Formatação ───────────────────────────────────────────────────────────────

def formatar_boletim():
    dados = carregar_dados()
    eh_exemplo = dados.get("fonte") == "exemplo"

    # Prioridade: publicado_em (data real do boletim CPRH) > atualizado_em (fallback)
    publicado_em = dados.get("publicado_em")
    if publicado_em:
        data = publicado_em  # já formatado dd/mm/yyyy
    else:
        data = datetime.fromisoformat(dados["atualizado_em"]).strftime("%d/%m/%Y")

    boletim_nr = dados.get("boletim_nr", "")
    periodo = dados.get("periodo", "")

    cabecalho = f"📅 Boletim {boletim_nr}" if boletim_nr else "📅 Boletim"
    cabecalho += f" — publicado em {data}"
    if periodo:
        cabecalho += f"\n🗓️ Vigência: {periodo}"

    linhas = [
        "🏖️ BALNEABILIDADE — PERNAMBUCO",
        cabecalho,
        f"✅ Próprias: {dados['total_proprias']}  |  ❌ Impróprias: {dados['total_improprias']}",
        ""
    ]

    melhora = dados.get("alteracoes_melhora", [])
    piora = dados.get("alteracoes_piora", [])
    if dados.get("sem_alteracoes"):
        linhas.append("ℹ️ Sem alterações em relação ao boletim anterior.")
        linhas.append("")
    elif melhora or piora:
        if melhora:
            linhas.append(f"📈 Melhoraram ({len(melhora)}): {', '.join(melhora)}")
        if piora:
            linhas.append(f"📉 Pioraram ({len(piora)}): {', '.join(piora)}")
        linhas.append("")

    municipios = {}
    for p in dados["praias"]:
        mun = p.get("municipio") or "Outras"
        municipios.setdefault(mun, []).append(p)

    for mun, lista in municipios.items():
        linhas.append(f"📍 {mun.upper()}")
        for p in lista:
            icone = "🟢" if p["status"] == "PRÓPRIA" else "🔴"
            linhas.append(f"  {icone} {p['praia']}")
        linhas.append("")

    linhas.append("📊 Fonte: CPRH — cprh.pe.gov.br")
    linhas.append("⚠️ Evite o mar 24h após chuvas fortes")

    return "\n".join(linhas)


# ─── Bot Telegram ─────────────────────────────────────────────────────────────

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    texto = (update.message.text or "").strip()

    # Registra como assinante automaticamente em qualquer interação
    adicionar_assinante(chat_id)

    # Comandos admin (só funcionam para ADMIN_CHAT_ID)
    if chat_id == ADMIN_CHAT_ID and texto.startswith("/"):
        if texto == "/forcar_broadcast":
            await update.message.reply_text("🔔 Forçando broadcast...")
            await broadcast_boletim(context.application)
            await update.message.reply_text("✅ Broadcast concluído.")
            return
        if texto == "/forcar_atualizacao":
            await update.message.reply_text("🔄 Tentando buscar PDF da CPRH...")
            atualizar_dados()
            await verificar_e_notificar(context.application)
            await update.message.reply_text("✅ Concluído.")
            return
        if texto == "/stats":
            n = len(carregar_assinantes())
            d = carregar_dados()
            await update.message.reply_text(
                f"👥 Assinantes: {n}\n"
                f"📅 Boletim: {d.get('boletim_nr','?')}\n"
                f"✅ Próprias: {d.get('total_proprias','?')}\n"
                f"❌ Impróprias: {d.get('total_improprias','?')}\n"
                f"📊 Fonte: {d.get('fonte','?')}"
            )
            return

    await update.message.reply_text(formatar_boletim())


def main():
    import urllib3
    import asyncio
    urllib3.disable_warnings()

    # Inicia HTTP server para Azure App Service F1 health checks
    iniciar_http_server()

    # Se o exemplo é mais recente que os dados em disco, apaga o cache
    exemplo = dados_exemplo()
    if DADOS_FILE.exists():
        try:
            dados_disco = json.loads(DADOS_FILE.read_text(encoding="utf-8"))
            data_disco = dados_disco.get("atualizado_em", "2000-01-01")
            data_exemplo = exemplo.get("atualizado_em", "2000-01-01")
            if data_exemplo > data_disco:
                DADOS_FILE.unlink()
                log.info(f"🗑️ Cache apagado: exemplo ({data_exemplo}) mais recente que disco ({data_disco})")
        except Exception as e:
            log.warning(f"Erro ao verificar cache: {e}")

    # Tenta buscar dados reais da CPRH
    atualizar_dados()

    # Garante que temos dados (reais ou exemplo)
    carregar_dados()

    # Container para guardar referência ao loop (preenchido em post_init)
    app_loop = {"loop": None}

    async def post_init(application):
        """Captura o event loop após inicialização e roda 1ª verificação."""
        app_loop["loop"] = asyncio.get_running_loop()
        # Verifica se exemplo embutido (Boletim novo no código) deve gerar broadcast
        await verificar_e_notificar(application)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL, responder))

    # Wrapper síncrono para chamar verificar_e_notificar do scheduler
    def job_atualizar_e_notificar():
        try:
            atualizar_dados()
            loop = app_loop.get("loop")
            if loop is None:
                log.warning("⚠️ Loop ainda não disponível, pulando broadcast")
                return
            # Agenda a corrotina no event loop do bot
            asyncio.run_coroutine_threadsafe(
                verificar_e_notificar(app),
                loop
            )
        except Exception as e:
            log.error(f"Erro no job automático: {e}")

    # Agenda atualizações:
    # - Diário às 14h e 18h (America/Recife): tenta baixar PDF da CPRH
    # - Self-ping a cada 14 min p/ manter Azure F1 acordado
    scheduler = BackgroundScheduler(timezone="America/Recife")
    scheduler.add_job(job_atualizar_e_notificar, "cron", hour=14, minute=0)
    scheduler.add_job(job_atualizar_e_notificar, "cron", hour=18, minute=0)
    scheduler.add_job(self_ping, "interval", minutes=14)
    scheduler.start()
    log.info("⏰ Agendamento: diário 14h e 18h (America/Recife) + self-ping 14min")

    log.info("🤖 Bot iniciado!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
