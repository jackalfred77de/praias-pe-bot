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
DADOS_FILE = Path.home() / "dados_praias.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_praias")

# Todas as 27 praias monitoradas pela CPRH com seus municípios
PRAIAS_CONHECIDAS = [
    {"praia": "Jaguaribe", "municipio": "Itamaracá"},
    {"praia": "Pilar", "municipio": "Itamaracá"},
    {"praia": "Forte Orange", "municipio": "Itamaracá"},
    {"praia": "Maria Farinha", "municipio": "Paulista"},
    {"praia": "Janga (Cond. Roberto Barbosa)", "municipio": "Paulista"},
    {"praia": "Janga (Rua Betânia)", "municipio": "Paulista"},
    # Região Metropolitana - Olinda/Recife
    {"praia": "Rio Doce", "municipio": "Olinda"},
    {"praia": "Bairro Novo", "municipio": "Olinda"},
    {"praia": "Carmo", "municipio": "Olinda"},
    {"praia": "Milagres", "municipio": "Olinda"},
    {"praia": "Pina", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 8)", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 15)", "municipio": "Recife"},
    # Litoral Sul - Jaboatão
    {"praia": "Piedade", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Candeias (Conj. Candeias II)", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Candeias (Rest. Candelária)", "municipio": "Jaboatão dos Guararapes"},
    {"praia": "Barra de Jangadas", "municipio": "Jaboatão dos Guararapes"},
    # Igarassu
    {"praia": "Praia do Capitão (Mangue Seco)", "municipio": "Igarassu"},
    # Litoral Sul - Cabo / Ipojuca
    {"praia": "Enseada dos Corais", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Gaibu", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Suape", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Porto de Galinhas", "municipio": "Ipojuca"},
    {"praia": "Ponta de Serrambi", "municipio": "Ipojuca"},
    # Litoral Sul - Tamandaré
    {"praia": "Praia dos Carneiros", "municipio": "Tamandaré"},
    {"praia": "Tamandaré (Hotel Marinas)", "municipio": "Tamandaré"},
    {"praia": "Tamandaré (Rua Nilo Gouveia)", "municipio": "Tamandaré"},
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
        pdf_path = Path.home() / "temp_balneabilidade.pdf"
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
    # Boletim 16/2026 — Período 17/04 a 23/04, coleta 13/04
    # 14 próprias / 13 impróprias
    status_map = {
        "Jaguaribe": "IMPRÓPRIA",
        "Pilar": "PRÓPRIA",
        "Forte Orange": "PRÓPRIA",
        "Maria Farinha": "IMPRÓPRIA",
        "Janga (Cond. Roberto Barbosa)": "IMPRÓPRIA",
        "Janga (Rua Betânia)": "IMPRÓPRIA",
        "Rio Doce": "IMPRÓPRIA",
        "Bairro Novo": "IMPRÓPRIA",
        "Carmo": "IMPRÓPRIA",
        "Milagres": "IMPRÓPRIA",
        "Pina": "IMPRÓPRIA",
        "Boa Viagem (Posto 8)": "PRÓPRIA",
        "Boa Viagem (Posto 15)": "PRÓPRIA",
        "Piedade": "PRÓPRIA",
        "Candeias (Conj. Candeias II)": "PRÓPRIA",
        "Candeias (Rest. Candelária)": "IMPRÓPRIA",
        "Barra de Jangadas": "IMPRÓPRIA",
        "Enseada dos Corais": "IMPRÓPRIA",          # ← piorou (era PRÓPRIA no bol. 15)
        "Gaibu": "PRÓPRIA",                          # ← melhorou (era IMPRÓPRIA no bol. 15)
        "Suape": "IMPRÓPRIA",
        "Porto de Galinhas": "PRÓPRIA",
        "Ponta de Serrambi": "PRÓPRIA",
        "Praia dos Carneiros": "PRÓPRIA",
        "Tamandaré (Hotel Marinas)": "PRÓPRIA",
        "Tamandaré (Rua Nilo Gouveia)": "PRÓPRIA",
        "Praia do Capitão (Mangue Seco)": "PRÓPRIA",
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
        "atualizado_em": "2026-04-17T00:00:00",
        "boletim_nr": "16/2026",
        "alteracoes_melhora": ["Gaibu"],
        "alteracoes_piora": ["Enseada dos Corais"],
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


# ─── Formatação ───────────────────────────────────────────────────────────────

def formatar_boletim():
    dados = carregar_dados()
    data = datetime.fromisoformat(dados["atualizado_em"]).strftime("%d/%m/%Y")
    eh_exemplo = dados.get("fonte") == "exemplo"

    linhas = [
        "🏖️ BALNEABILIDADE — PERNAMBUCO",
        f"📅 Boletim de: {data}" + (f" _(ref. {dados['boletim_nr']})_" if eh_exemplo and dados.get("boletim_nr") else (" _(referência)_" if eh_exemplo else "")),
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
    await update.message.reply_text(formatar_boletim())


def main():
    import urllib3
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

    # Agenda atualização toda sexta às 14h
    scheduler = BackgroundScheduler()
    scheduler.add_job(atualizar_dados, "cron", day_of_week="fri", hour=14, minute=0)
    scheduler.add_job(self_ping, "interval", minutes=14)
    log.info("Self-ping a cada 14 min ativado")
    scheduler.start()
    log.info("⏰ Agendamento: toda sexta às 14h")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, responder))
    log.info("🤖 Bot iniciado!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
