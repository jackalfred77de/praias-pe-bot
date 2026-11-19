# Bot Praias PE (@praias_pe_bot)

Bot de Telegram com a balneabilidade semanal da CPRH (Pernambuco): 27 pontos de coleta de
Itamaracá a São José da Coroa Grande. Irmão do bot de SP: github.com/jackalfred77de/praias-sp-bot (privado).

## Regras

- **Este repositório é PÚBLICO.** Nunca gravar token, chat_id de usuário ou dado pessoal no código.
  O token antigo que esteve no histórico do git (removido em ad36370) já foi revogado.
- **Nunca** imprimir, registrar em log nem gravar em arquivo versionado nenhum token do Telegram.
  O token só existe na app setting `TELEGRAM_TOKEN` do Azure. Para trocar, o Fred cola o token
  num `read -s` no terminal; o Claude nunca vê o valor.
- Ao abrir o @BotFather no navegador, não tirar captura da conversa: mensagens antigas mostram tokens.
- Responder ao Fred em português, registro formal e conciso.

## Onde roda

- Azure App Service `praias-pe-bot`, grupo `rg-praias-bot`, plano F1 `plan-praias-bot`, Brazil South.
- App settings: `TELEGRAM_TOKEN`, `ADMIN_CHAT_ID`, `SCM_DO_BUILD_DURING_DEPLOYMENT`, `WEBSITES_ENABLE_APP_SERVICE_STORAGE`.
- Dados persistentes em `/home/data` (caminho fixo no código): `dados_praias.json`, `historico_status.json`,
  `assinantes.json`, `descadastrados.json`, `ultima_assinatura.txt`, `ultimo_diagnostico.txt`.
- Fonte: PDFs semanais em https://www2.cprh.pe.gov.br/monitoramento-ambiental/balneabilidade/informativos-semanais-AAAA/

## Deploy

Integração manual com o GitHub (branch `main`), build pelo Oryx:

1. `git push`
2. `python3 -m azure.cli webapp deployment source sync -g rg-praias-bot -n praias-pe-bot`
   (neste Mac: `/Library/Frameworks/Python.framework/Versions/3.11/bin/python3 -m azure.cli`)
3. Acompanhar `.../api/deployments/<sha completo>` no Kudu até `status` = 4.
4. Conferir `ultimo_diagnostico.txt` pela API do Kudu (`/api/vfs/data/ultimo_diagnostico.txt`,
   `--resource https://management.azure.com/`).

O app roda de `output.tar.zst` extraído em `/tmp`; o `bot_praias.py` solto em `wwwroot` é antigo e não é usado.

## Scraper

`encontrar_pdf_cprh()` lista os PDFs da página do ano e ordena pelo número no nome (`NN_AAAA`).
PDFs sem número no nome só entram se foram enviados (`/uploads/AAAA/MM/`) no mesmo mês do maior
numerado ou depois. `atualizar_dados()` abre os candidatos em ordem, fica com o **maior número de
boletim lido dentro do PDF** e para no primeiro numerado que funcionar. Nunca sobrescreve com boletim
mais antigo que o salvo.

Armadilha: a página tem `2026/05/informativo-balneabilidadpdf.pdf` (boletim 20/2026, nome quebrado).
Até 15/11/2026 ele entrava primeiro na fila e bloqueava a atualização.

## Comparação com a semana anterior

`historico_status.json` guarda `{"atual": {"nr", "status"}, "anterior": {...}}`, com status por
**código do ponto** (ITA-10, REC-50…), porque vários pontos têm o mesmo nome (Candeias, Boa Viagem,
Janga, Tamandaré). Quando chega boletim novo, o atual vira anterior. A mensagem da cidade marca
`📈 melhorou` / `📉 piorou` / `➖ igual` e diz contra qual boletim compara; pontos com nome repetido
mostram o local entre parênteses (`nome_exibicao`).

Para reconstruir o anterior: baixar o PDF do boletim anterior da CPRH, ler com `scrape_pdf`
(mockando `requests.get` para o arquivo local), validar o boletim atual contra `dados_praias.json`
e gravar via Kudu VFS (`PUT .../api/vfs/data/historico_status.json` com `If-Match: *`).

## Funcionalidades

- Qualquer mensagem: boletim completo por município, melhoraram/pioraram, botões por município.
- Botão de município: praias do município com a comparação acima.
- Inscrição automática com aviso de privacidade (LGPD); `/parar` (ou botão) e `/assinar`.
- Broadcast quando o boletim muda. Admin (`ADMIN_CHAT_ID`): `/forcar_atualizacao`, `/forcar_broadcast`, `/stats`, `/diagnostico`.
- WhatsApp: `/wa.json` (lido pelo app `praias-wa-bot`, código no repositório privado praias-sp-bot, pasta `whatsapp/`).
  No broadcast o admin recebe o texto pronto para o Canal do WhatsApp; `/canal` repete. App setting opcional `WA_LINK`.

## Hibernação do F1

O F1 dorme sem acesso HTTP e o bot descarta mensagens pendentes ao acordar. A tarefa agendada local
`acordar-bots-praias` acessa as duas URLs a cada 20 min (só com o app do Claude aberto). Solução
definitiva sugerida e não decidida: plano B1 com Always On.

## Histórico

- **15/11/2026** — Token do bot removido do código (ad36370) e depois revogado; token novo só no Azure.
  `httpx` silenciado (gravava o token no log a cada 10 s). LGPD (`/parar`, `/assinar`, aviso).
  Menu de botões por município. Comparação com a semana anterior por código de ponto, com o
  boletim 45/2026 reconstruído do PDF da CPRH. Scraper corrigido (PDF velho de nome quebrado).
  Cópia na Render suspensa (auto deploy desligado); Railway offline.
