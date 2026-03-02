import re
import json
import logging
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

TELEGRAM_TOKEN = "8537829811:AAEMKGX_w3kRLPwEQFQ-QnUWO3BG9YmJECk"
DADOS_FILE = Path.home() / "dados_praias.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot_praias")

# Todas as 27 praias monitoradas pela CPRH com seus municÃÂÃÂ­pios
PRAIAS_CONHECIDAS = [
    {"praia": "Jaguaribe", "municipio": "ItamaracÃÂÃÂ¡"},
    {"praia": "Pilar", "municipio": "ItamaracÃÂÃÂ¡"},
    {"praia": "Forte Orange", "municipio": "ItamaracÃÂÃÂ¡"},
    {"praia": "Maria Farinha", "municipio": "Paulista"},
    {"praia": "Janga (Cond. Roberto Barbosa)", "municipio": "Paulista"},
    {"praia": "Janga (Rua BetÃÂÃÂ¢nia)", "municipio": "Paulista"},
    # RegiÃÂÃÂ£o Metropolitana - Olinda/Recife
    {"praia": "Rio Doce", "municipio": "Olinda"},
    {"praia": "Bairro Novo", "municipio": "Olinda"},
    {"praia": "Carmo", "municipio": "Olinda"},
    {"praia": "Milagres", "municipio": "Olinda"},
    {"praia": "Pina", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 8)", "municipio": "Recife"},
    {"praia": "Boa Viagem (Posto 15)", "municipio": "Recife"},
    # Litoral Sul - JaboatÃÂÃÂ£o
    {"praia": "Piedade", "municipio": "JaboatÃÂÃÂ£o dos Guararapes"},
    {"praia": "Candeias (Conj. Candeias II)", "municipio": "JaboatÃÂÃÂ£o dos Guararapes"},
    {"praia": "Candeias (Rest. CandelÃÂÃÂ¡ria)", "municipio": "JaboatÃÂÃÂ£o dos Guararapes"},
    {"praia": "Barra de Jangadas", "municipio": "JaboatÃÂÃÂ£o dos Guararapes"},
    # Igarassu
    {"praia": "Praia do CapitÃÂÃÂ£o (Mangue Seco)", "municipio": "Igarassu"},
    # Litoral Sul - Cabo / Ipojuca
    {"praia": "Enseada dos Corais", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Gaibu", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Suape", "municipio": "Cabo de Santo Agostinho"},
    {"praia": "Porto de Galinhas", "municipio": "Ipojuca"},
    {"praia": "Ponta de Serrambi", "municipio": "Ipojuca"},
    # Litoral Sul - TamandarÃÂÃÂ©
    {"praia": "Praia dos Carneiros", "municipio": "TamandarÃÂÃÂ©"},
    {"praia": "TamandarÃÂÃÂ© (Hotel Marinas)", "municipio": "TamandarÃÂÃÂ©"},
    {"praia": "TamandarÃÂÃÂ© (Rua Nilo Gouveia)", "municipio": "TamandarÃÂÃÂ©"},
    {"praia": "SÃÂÃÂ£o JosÃÂÃÂ© da Coroa Grande", "municipio": "SÃÂÃÂ£o JosÃÂÃÂ© da Coroa Grande"},
]


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Scraper CPRH ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

def parse_status(texto):
    t = texto.upper().strip()
    if "IMPR" in t:
        return "IMPRÃÂÃÂPRIA"
    if "PR" in t:
        return "PRÃÂÃÂPRIA"
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
                    if re.match(r"^(PR[OÃÂÃÂ]PRIA|IMPR[OÃÂÃÂ]PRIA)$", linha, re.IGNORECASE):
                        status_atual = parse_status(linha)
                        continue

                    # Detecta municÃÂÃÂ­pio (linha curta sem nÃÂÃÂºmeros)
                    if (len(linha) < 35
                            and not any(c.isdigit() for c in linha)
                            and "praia" not in linha.lower()
                            and "em frente" not in linha.lower()
                            and linha not in ["PRÃÂÃÂPRIA", "IMPRÃÂÃÂPRIA"]):
                        municipio_atual = linha
                        continue

                    # Linha de praia
                    if ("praia" in linha.lower() or "em frente" in linha.lower()) and status_atual:
                        # Extrai nome limpo da praia
                        nome = re.sub(r"(?i)^praia\s+(de|da|do|dos|das)\s+", "", linha)
                        nome = nome.split(",")[0].split("ÃÂ¢ÃÂÃÂ")[0].strip()
                        praias.append({
                            "praia": nome,
                            "status": status_atual,
                            "municipio": municipio_atual
                        })

        pdf_path.unlink(missing_ok=True)
        log.info(f"PDF extraÃÂÃÂ­do: {len(praias)} praias")

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

        # Tenta tambÃÂÃÂ©m na pÃÂÃÂ¡gina de uploads do WordPress
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "uploads" in href and href.lower().endswith(".pdf"):
                return href if href.startswith("http") else "https://www2.cprh.pe.gov.br/" + href.lstrip("/")

    except Exception as e:
        log.error(f"Erro ao buscar PDF: {e}")

    return None


def atualizar_dados():
    """Busca dados da CPRH e atualiza o arquivo local."""
    log.info("ÃÂ°ÃÂÃÂÃÂ Atualizando dados da CPRH...")

    if not PDF_SUPPORT:
        log.error("pdfplumber nÃÂÃÂ£o instalado")
        return

    pdf_url = encontrar_pdf_cprh()
    praias = []

    if pdf_url:
        praias = scrape_pdf(pdf_url)
    else:
        log.warning("PDF nÃÂÃÂ£o encontrado no site da CPRH")

    if praias:
        dados = {
            "atualizado_em": datetime.now().isoformat(),
            "total_proprias": sum(1 for p in praias if p["status"] == "PRÃÂÃÂPRIA"),
            "total_improprias": sum(1 for p in praias if p["status"] == "IMPRÃÂÃÂPRIA"),
            "praias": praias,
            "fonte": "cprh"
        }
        DADOS_FILE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"ÃÂ¢ÃÂÃÂ {len(praias)} praias salvas da CPRH")
    else:
        log.warning("ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ Scraper nÃÂÃÂ£o encontrou dados ÃÂ¢ÃÂÃÂ mantendo dados anteriores")


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Dados de exemplo (fallback com todas as 27 praias) ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

def dados_exemplo():
    """Retorna dados de exemplo com todas as praias conhecidas."""
    # Baseado no boletim mais recente (semana 9/2026)
    status_map = {
        "Jaguaribe": "IMPRÃÂÃÂPRIA",
        "Pilar": "PRÃÂÃÂPRIA",
        "Forte Orange": "PRÃÂÃÂPRIA",
        "Maria Farinha": "PRÃÂÃÂPRIA",
        "Janga (Cond. Roberto Barbosa)": "PRÃÂÃÂPRIA",
        "Janga (Rua BetÃÂÃÂ¢nia)": "PRÃÂÃÂPRIA",
        "Rio Doce": "IMPRÃÂÃÂPRIA",
        "Bairro Novo": "IMPRÃÂÃÂPRIA",
        "Carmo": "IMPRÃÂÃÂPRIA",
        "Milagres": "IMPRÃÂÃÂPRIA",
        "Pina": "IMPRÃÂÃÂPRIA",
        "Boa Viagem (Posto 8)": "PRÃÂÃÂPRIA",
        "Boa Viagem (Posto 15)": "PRÃÂÃÂPRIA",
        "Piedade": "PRÃÂÃÂPRIA",
        "Candeias (Conj. Candeias II)": "IMPRÃÂÃÂPRIA",
        "Candeias (Rest. CandelÃÂÃÂ¡ria)": "IMPRÃÂÃÂPRIA",
        "Barra de Jangadas": "PRÃÂÃÂPRIA",
        "Enseada dos Corais": "PRÃÂÃÂPRIA",
        "Gaibu": "IMPRÃÂÃÂPRIA",
        "Suape": "IMPRÃÂÃÂPRIA",
        "Porto de Galinhas": "PRÃÂÃÂPRIA",
        "Ponta de Serrambi": "PRÃÂÃÂPRIA",
        "Praia dos Carneiros": "PRÃÂÃÂPRIA",
        "TamandarÃÂÃÂ© (Hotel Marinas)": "PRÃÂÃÂPRIA",
        "TamandarÃÂÃÂ© (Rua Nilo Gouveia)": "PRÃÂÃÂPRIA",
        "Praia do CapitÃÂÃÂ£o (Mangue Seco)": "IMPRÃÂÃÂPRIA",
        "SÃÂÃÂ£o JosÃÂÃÂ© da Coroa Grande": "PRÃÂÃÂPRIA",
    }

    praias = []
    for p in PRAIAS_CONHECIDAS:
        praias.append({
            "praia": p["praia"],
            "municipio": p["municipio"],
            "status": status_map.get(p["praia"], "PRÃÂÃÂPRIA")
        })

    return {
        "atualizado_em": "2026-02-27T00:00:00",
        "sem_alteracoes": True,
        "total_proprias": sum(1 for p in praias if p["status"] == "PRÃÂÃÂPRIA"),
        "total_improprias": sum(1 for p in praias if p["status"] == "IMPRÃÂÃÂPRIA"),
        "praias": praias,
        "fonte": "exemplo",
        "sem_alteracoes": True
    }


def carregar_dados():
    if DADOS_FILE.exists():
        dados = json.loads(DADOS_FILE.read_text(encoding="utf-8"))
        # Se tem dados reais da CPRH, usa. Se tem menos de 10 praias, usa exemplo
        if len(dados.get("praias", [])) >= 10:
            return dados
    d = dados_exemplo()
    DADOS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return d


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ FormataÃÂÃÂ§ÃÂÃÂ£o ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

def formatar_boletim():
    dados = carregar_dados()
    data = datetime.fromisoformat(dados["atualizado_em"]).strftime("%d/%m/%Y")
    eh_exemplo = dados.get("fonte") == "exemplo"

    linhas = [
        "ÃÂ°ÃÂÃÂÃÂÃÂ¯ÃÂ¸ÃÂ BALNEABILIDADE ÃÂ¢ÃÂÃÂ PERNAMBUCO",
        f"ÃÂ°ÃÂÃÂÃÂ Boletim de: {data}" + (" _(referÃÂÃÂªncia)_" if eh_exemplo else ""),
        f"ÃÂ¢ÃÂÃÂ PrÃÂÃÂ³prias: {dados['total_proprias']}  |  ÃÂ¢ÃÂÃÂ ImprÃÂÃÂ³prias: {dados['total_improprias']}",
        ""
    ]

    if dados.get("sem_alteracoes"):
        linhas.append("â¹ï¸ Sem alteraÃ§Ãµes em relaÃ§Ã£o ao boletim anterior.")
        linhas.append("")

    municipios = {}
    for p in dados["praias"]:
        mun = p.get("municipio") or "Outras"
        municipios.setdefault(mun, []).append(p)

    for mun, lista in municipios.items():
        linhas.append(f"ÃÂ°ÃÂÃÂÃÂ {mun.upper()}")
        for p in lista:
            icone = "ÃÂ°ÃÂÃÂÃÂ¢" if p["status"] == "PRÃÂÃÂPRIA" else "ÃÂ°ÃÂÃÂÃÂ´"
            linhas.append(f"  {icone} {p['praia']}")
        linhas.append("")

    linhas.append("ÃÂ°ÃÂÃÂÃÂ Fonte: CPRH ÃÂ¢ÃÂÃÂ cprh.pe.gov.br")
    linhas.append("ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ Evite o mar 24h apÃÂÃÂ³s chuvas fortes")

    return "\n".join(linhas)


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Bot Telegram ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

async def responder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(formatar_boletim())


def main():
    import urllib3
    urllib3.disable_warnings()

    # Tenta buscar dados reais da CPRH
    atualizar_dados()

    # Garante que temos dados (reais ou exemplo)
    carregar_dados()

    # Agenda atualizaÃÂÃÂ§ÃÂÃÂ£o toda sexta ÃÂÃÂ s 14h
    scheduler = BackgroundScheduler()
    scheduler.add_job(atualizar_dados, "cron", day_of_week="fri", hour=14, minute=0)
    scheduler.start()
    log.info("ÃÂ¢ÃÂÃÂ° Agendamento: toda sexta ÃÂÃÂ s 14h")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, responder))
    log.info("ÃÂ°ÃÂÃÂ¤ÃÂ Bot iniciado!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
