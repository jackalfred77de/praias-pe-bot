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
        log.info(f"📥 Baixando PDF: {pdf_url}")
        resp = requests.get(pdf_url, timeout=30, verify=False)
        log.info(f"   PDF download: HTTP {resp.status_code}, {len(resp.content)} bytes")

        if resp.status_code != 200:
            registrar_diagnostico(f"PDF download falhou: HTTP {resp.status_code} para {pdf_url}")
            return []
        if len(resp.content) < 1000:
            registrar_diagnostico(f"PDF muito pequeno ({len(resp.content)} bytes), provavelmente erro: {resp.text[:200]}")
            return []

        pdf_path = DADOS_DIR / "temp_balneabilidade.pdf"
        pdf_path.write_bytes(resp.content)

        status_atual = None
        municipio_atual = ""

        with pdfplumber.open(pdf_path) as pdf:
            log.info(f"   PDF aberto: {len(pdf.pages)} páginas")
            todo_texto = ""
            for page in pdf.pages:
                texto = page.extract_text() or ""
                todo_texto += texto + "\n"

            # ── PARSER NOVO (formato 2024+): tudo numa linha por praia ──
            # Padrão típico: "ITA-10 Praia de Pilar, em frente à Igreja do Pilar. Itamaracá Imprópria"
            # Codigo de coleta: 3 letras + hífen + 2 dígitos
            padrao_linha = re.compile(
                r"^([A-Z]{3}-\d{2,3})\s+(.+?)\s+(Pr[óo]pria|Impr[óo]pria)\s*$",
                re.IGNORECASE
            )

            # Lista de municípios conhecidos para separar do nome da praia
            municipios_conhecidos = [
                "Itamaracá", "Igarassu", "Paulista", "Olinda", "Recife",
                "Jaboatão dos Guararapes", "Cabo de Sto Agostinho",
                "Cabo de Santo Agostinho", "Ipojuca", "Tamandaré",
                "São José da C. Grande", "São José da Coroa Grande"
            ]

            for linha in todo_texto.splitlines():
                linha = linha.strip()
                if not linha:
                    continue

                m = padrao_linha.match(linha)
                if not m:
                    continue

                codigo = m.group(1).upper()
                meio = m.group(2).strip()
                status = parse_status(m.group(3))

                # Separa município do nome da praia (município sempre no fim do "meio")
                municipio = ""
                nome_praia = meio
                for mun in municipios_conhecidos:
                    if meio.endswith(" " + mun) or meio.endswith("." + mun):
                        municipio = mun
                        nome_praia = meio[: len(meio) - len(mun)].rstrip(" .")
                        break

                # Extrai nome limpo da praia (remove "Praia de/do/da/dos")
                nome_limpo = re.sub(r"(?i)^praia\s+(de|da|do|dos|das)\s+", "", nome_praia)
                # Tira a parte após a vírgula (descrição da localização)
                nome_limpo = nome_limpo.split(",")[0].split("–")[0].strip()

                praias.append({
                    "codigo": codigo,
                    "praia": nome_limpo,
                    "praia_completa": nome_praia,
                    "status": status,
                    "municipio": municipio
                })

            # Tenta extrair metadados (boletim_nr, periodo, coleta) do cabeçalho
            metadados = {}
            m_nr = re.search(r"INFORMATIVO\s+N[ºo°]?:?\s*(\d{1,2}/\d{4})", todo_texto, re.IGNORECASE)
            if m_nr:
                metadados["boletim_nr"] = m_nr.group(1)
            m_data = re.search(r"DATA:\s*(\d{2}/\d{2}/\d{4})", todo_texto, re.IGNORECASE)
            if m_data:
                metadados["publicado_em"] = m_data.group(1)
            m_per = re.search(r"PER[ÍI]ODO:\s*(\d{2}/\d{2}/\d{4})\s+a\s+(\d{2}/\d{2}/\d{4})", todo_texto, re.IGNORECASE)
            if m_per:
                # Compacta para "08/05 a 14/05"
                d1 = m_per.group(1)[:5]
                d2 = m_per.group(2)[:5]
                metadados["periodo"] = f"{d1} a {d2}"
            m_col = re.search(r"DATA\s+DA\s+COLETA:\s*(\d{2}/\d{2}/\d{4})", todo_texto, re.IGNORECASE)
            if m_col:
                metadados["coleta"] = m_col.group(1)

            # Anexa metadados na primeira praia para o caller poder acessar
            if praias and metadados:
                praias[0]["_metadados"] = metadados

            # Se não extraiu nada, salva amostra do texto para diagnóstico
            if not praias:
                amostra = todo_texto[:1500].replace("\n", " | ")
                registrar_diagnostico(f"PARSER FALHOU. Texto extraído ({len(todo_texto)} chars), amostra: {amostra}")

        pdf_path.unlink(missing_ok=True)
        log.info(f"📊 PDF extraído: {len(praias)} praias")

    except requests.exceptions.SSLError as e:
        msg = f"SSLError no PDF (geo-block ou cert): {str(e)[:200]}"
        log.error(msg)
        registrar_diagnostico(msg)
    except requests.exceptions.Timeout as e:
        msg = f"Timeout no PDF: {e}"
        log.error(msg)
        registrar_diagnostico(msg)
    except requests.exceptions.ConnectionError as e:
        msg = f"ConnectionError no PDF: {str(e)[:200]}"
        log.error(msg)
        registrar_diagnostico(msg)
    except Exception as e:
        msg = f"Erro ao processar PDF ({type(e).__name__}): {str(e)[:200]}"
        log.error(msg)
        registrar_diagnostico(msg)

    return praias


def registrar_diagnostico(mensagem):
    """Salva diagnóstico em arquivo persistente para inspeção via Kudu."""
    try:
        from datetime import datetime as _dt
        diag_file = DADOS_DIR / "ultimo_diagnostico.txt"
        timestamp = _dt.now().isoformat()
        with diag_file.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {mensagem}\n")
        # Limita o arquivo a últimas 100 linhas
        if diag_file.stat().st_size > 50000:
            linhas = diag_file.read_text(encoding="utf-8").splitlines()
            diag_file.write_text("\n".join(linhas[-100:]), encoding="utf-8")
    except Exception as e:
        log.error(f"Erro ao escrever diagnóstico: {e}")


def encontrar_pdf_cprh():
    """Busca o link do PDF mais recente no site da CPRH."""
    ano_atual = datetime.now().year
    urls_para_tentar = [
        # Página específica do ano corrente (formato CPRH 2024+)
        f"https://www2.cprh.pe.gov.br/monitoramento-ambiental/balneabilidade/informativos-semanais-{ano_atual}/",
        # Fallbacks
        "https://www2.cprh.pe.gov.br/monitoramento-ambiental/balneabilidade/",
        "https://www2.cprh.pe.gov.br/monitoramento-ambiental/balneabilidade/informativo-semanal/",
    ]
    user_agents = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ]

    for url in urls_para_tentar:
        for ua in user_agents:
            try:
                log.info(f"🔍 Tentando: {url} (UA={ua[:30]}...)")
                resp = requests.get(
                    url,
                    headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
                    timeout=15, verify=False
                )
                log.info(f"   HTTP {resp.status_code}, {len(resp.text)} chars")

                if resp.status_code != 200:
                    registrar_diagnostico(f"CPRH page {url}: HTTP {resp.status_code}")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                pdf_links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.lower().endswith(".pdf") and ("balneabilidade" in href.lower() or "informativo" in href.lower() or "uploads" in href.lower()):
                        full = href if href.startswith("http") else "https://www2.cprh.pe.gov.br/" + href.lstrip("/")
                        pdf_links.append(full)

                log.info(f"   PDFs encontrados: {len(pdf_links)}")

                if pdf_links:
                    registrar_diagnostico(f"Total PDFs em {url}: {len(pdf_links)}")
                    for p in pdf_links[:5]:
                        registrar_diagnostico(f"  PDF: {p}")

                # Heurística: filtra por ano atual e pega o boletim de número mais alto
                import re as _re
                ano_pdfs = [p for p in pdf_links if str(ano_atual) in p]

                if ano_pdfs:
                    # Padrão CPRH 2024+: "informativo-balneabilidade-19_2026-enterolert.pdf"
                    # Padrão antigo:     "INFORMATIVO_DA_BANEABILIDADE_DAS_PRAIAS_DE_PERNAMBUCO_19_2026.pdf"
                    def num_boletim(url):
                        # Procura padrão "NN_AAAA" onde AAAA é o ano atual
                        m = _re.search(rf"[-_](\d{{1,2}})_{ano_atual}", url)
                        if m:
                            return int(m.group(1))
                        return 0
                    ano_pdfs.sort(key=num_boletim, reverse=True)
                    escolhido = ano_pdfs[0]
                    registrar_diagnostico(f"ESCOLHIDO (ano {ano_atual}, bol={num_boletim(escolhido)}): {escolhido}")
                    return escolhido

                if pdf_links:
                    # Última opção: primeiro PDF da página (geralmente o mais recente em listagens)
                    escolhido = pdf_links[0]
                    registrar_diagnostico(f"ESCOLHIDO (primeiro): {escolhido}")
                    return escolhido

            except requests.exceptions.SSLError as e:
                msg = f"SSLError {url}: {str(e)[:150]}"
                log.error(msg)
                registrar_diagnostico(msg)
            except requests.exceptions.Timeout:
                msg = f"Timeout {url}"
                log.error(msg)
                registrar_diagnostico(msg)
            except requests.exceptions.ConnectionError as e:
                msg = f"ConnectionError {url}: {str(e)[:150]}"
                log.error(msg)
                registrar_diagnostico(msg)
            except Exception as e:
                msg = f"Erro {url} ({type(e).__name__}): {str(e)[:150]}"
                log.error(msg)
                registrar_diagnostico(msg)

    registrar_diagnostico("Nenhum PDF encontrado em nenhuma URL/UA tentada")
    return None


def atualizar_dados():
    """Busca dados da CPRH e atualiza o arquivo local."""
    log.info("🔄 Atualizando dados da CPRH...")
    registrar_diagnostico("=== Iniciando atualização ===")

    if not PDF_SUPPORT:
        msg = "pdfplumber não instalado"
        log.error(msg)
        registrar_diagnostico(msg)
        return

    pdf_url = encontrar_pdf_cprh()
    praias = []

    if pdf_url:
        log.info(f"   Link encontrado: {pdf_url}")
        registrar_diagnostico(f"PDF localizado: {pdf_url}")
        praias = scrape_pdf(pdf_url)
    else:
        log.warning("PDF não encontrado no site da CPRH")

    if praias:
        # Extrai metadados que o scraper anexou na primeira praia
        metadados = praias[0].pop("_metadados", {}) if praias else {}

        dados = {
            "atualizado_em": datetime.now().isoformat(),
            "boletim_nr": metadados.get("boletim_nr", ""),
            "publicado_em": metadados.get("publicado_em", ""),
            "periodo": metadados.get("periodo", ""),
            "coleta": metadados.get("coleta", ""),
            "total_proprias": sum(1 for p in praias if p["status"] == "PRÓPRIA"),
            "total_improprias": sum(1 for p in praias if p["status"] == "IMPRÓPRIA"),
            "praias": praias,
            "fonte": "cprh"
        }
        DADOS_FILE.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"✅ {len(praias)} praias salvas da CPRH (Boletim {dados['boletim_nr']})")
        registrar_diagnostico(f"SUCESSO: Boletim {dados['boletim_nr']} - {len(praias)} praias salvas ({dados['total_proprias']} próprias / {dados['total_improprias']} impróprias)")
    else:
        log.warning("⚠️ Scraper não encontrou dados — mantendo dados anteriores")
        registrar_diagnostico("FALHA: scraper não retornou nenhuma praia")


# ─── Dados de exemplo (fallback com todas as 27 praias) ───────────────────────

def dados_exemplo():
    """Retorna dados de exemplo com todas as praias conhecidas."""
    # Boletim 19/2026 — Publicado 08/05/2026, vigência 08/05 a 14/05, coleta 04/05/2026
    # 11 próprias / 16 impróprias
    # Mudanças vs Bol. 18:
    #   PIORARAM: Maria Farinha, Boa Viagem (Posto 8)
    #   MELHORARAM: nenhuma
    status_map = {
        "Jaguaribe": "IMPRÓPRIA",
        "Pilar": "IMPRÓPRIA",
        "Forte Orange": "PRÓPRIA",
        "Praia do Capitão (Mangue Seco)": "PRÓPRIA",
        "Maria Farinha": "IMPRÓPRIA",                    # ← piorou
        "Janga (Cond. Roberto Barbosa)": "IMPRÓPRIA",
        "Janga (Rua Betânia)": "IMPRÓPRIA",
        "Rio Doce": "IMPRÓPRIA",
        "Bairro Novo": "IMPRÓPRIA",
        "Carmo": "IMPRÓPRIA",
        "Milagres": "IMPRÓPRIA",
        "Pina": "IMPRÓPRIA",
        "Boa Viagem (Posto 8)": "IMPRÓPRIA",             # ← piorou
        "Boa Viagem (Posto 15)": "PRÓPRIA",
        "Piedade": "PRÓPRIA",
        "Candeias (Conj. Candeias II)": "IMPRÓPRIA",     # (5422)
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
        "atualizado_em": "2026-05-09T00:00:00",
        "boletim_nr": "19/2026",
        "periodo": "08/05 a 14/05",
        "publicado_em": "08/05/2026",
        "coleta": "04/05/2026",
        "alteracoes_melhora": [],
        "alteracoes_piora": ["Maria Farinha", "Boa Viagem (Posto 8)"],
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
        if texto == "/diagnostico":
            diag_file = DADOS_DIR / "ultimo_diagnostico.txt"
            if diag_file.exists():
                conteudo = diag_file.read_text(encoding="utf-8")
                # Pega últimas 30 linhas
                linhas = conteudo.splitlines()[-30:]
                resp = "🔍 ÚLTIMAS 30 ENTRADAS DE DIAGNÓSTICO:\n\n" + "\n".join(linhas)
                # Telegram limit é 4096 chars
                if len(resp) > 4000:
                    resp = resp[-4000:]
                await update.message.reply_text(resp)
            else:
                await update.message.reply_text("Sem diagnóstico ainda. Rode /forcar_atualizacao primeiro.")
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
