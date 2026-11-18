#!/usr/bin/env python3
"""Exportador de vencimento de domínio via RDAP.

Serve /metrics em formato Prometheus com dias até cada domínio
expirar, para os alertas DominioVenceEm30Dias/DominioVenceEm7Dias
(prometheus/alert.rules.yml). RDAP, não WHOIS —
resposta em JSON estruturado, sem precisar do pacote `whois` (exigiria
apt install e root, e este processo roda como usuário comum).

Consulta RDAP no máximo uma vez a cada REFRESH_HORAS, nunca a cada scrape
do Prometheus (15s) — bater a API pública do registrador a cada 15s seria
abuso, e a maioria tem rate limit. O que fica em cache é a DATA de
expiração (fato), não "dias restantes": esse valor é recalculado a partir
do fato + agora em toda resposta, então se o refresh parar de funcionar
por qualquer motivo o metric continua correto (só para de se atualizar,
visível via dominio_rdap_ultima_consulta_timestamp) em vez de congelar
num número errado.

Uso: roda como serviço systemd --user, sem privilégio nenhum (porta alta,
127.0.0.1 só). Ver systemd/exportador-dominios.service.
"""

import json
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORTA = int(os.environ.get("PORTA", "9250"))
REFRESH_HORAS = 12

# RDAP é por TLD, não por domínio — cada registro (.com, .com.br, ...) tem
# seu próprio servidor. Lista pequena e fixa de propósito: só os TLDs dos
# domínios monitorados (ver DOMINIOS abaixo). Domínio novo de TLD ainda
# não visto aqui precisa achar o RDAP base dele primeiro (bootstrap oficial
# da IANA: https://data.iana.org/rdap/dns.json) e acrescentar no dict —
# falha alto e claro pra TLD desconhecido, não tenta adivinhar.
RDAP_BASE_POR_TLD = {
    "com": "https://rdap.verisign.com/com/v1/domain/",
    "com.br": "https://rdap.registro.br/domain/",
}

# Domínios a monitorar: variável de ambiente DOMINIOS (separados por vírgula).
DOMINIOS = [d.strip() for d in os.environ.get("DOMINIOS", "exemplo.com,exemplo.com.br").split(",") if d.strip()]


def tld_de(dominio: str) -> str:
    partes = dominio.split(".")
    # .com.br é o único TLD composto que os domínios deste exemplo usam hoje — pega
    # os dois últimos rótulos quando o segundo nível é com/net/org e o de
    # cima é um ccTLD de 2 letras, senão só o último rótulo.
    if len(partes) >= 3 and partes[-2] in {"com", "net", "org"} and len(partes[-1]) == 2:
        return ".".join(partes[-2:])
    return partes[-1]


def consultar_rdap(dominio: str) -> datetime:
    tld = tld_de(dominio)
    base = RDAP_BASE_POR_TLD.get(tld)
    if not base:
        raise ValueError(f"TLD '{tld}' sem RDAP base configurado (domínio {dominio}) — acrescentar em RDAP_BASE_POR_TLD")
    with urllib.request.urlopen(base + dominio, timeout=10) as resp:
        dado = json.loads(resp.read())
    for evento in dado.get("events", []):
        if evento.get("eventAction") == "expiration":
            # RDAP devolve ISO 8601 com "Z"; fromisoformat só aceita
            # "+00:00" antes do Python tratar "Z" nativamente (3.11+).
            return datetime.fromisoformat(evento["eventDate"].replace("Z", "+00:00"))
    raise ValueError(f"RDAP de {dominio} não trouxe evento 'expiration'")


# {dominio: (expiracao, consultado_em)} — lock porque o refresh roda numa
# thread de fundo enquanto requests HTTP chegam em paralelo
# (ThreadingHTTPServer).
cache: dict[str, tuple[datetime, datetime]] = {}
cache_lock = threading.Lock()


def atualizar_cache():
    for dominio in DOMINIOS:
        try:
            expiracao = consultar_rdap(dominio)
            with cache_lock:
                cache[dominio] = (expiracao, datetime.now(timezone.utc))
        except Exception as erro:
            # Falha num domínio não pode derrubar os outros nem travar o
            # laço — loga (jornal do systemd) e segue com o valor antigo
            # em cache, se houver, até a próxima tentativa.
            print(f"exportador-dominios: falha ao consultar {dominio}: {erro}", file=sys.stderr)


def laco_refresh():
    while True:
        atualizar_cache()
        time.sleep(REFRESH_HORAS * 3600)


def montar_metricas() -> str:
    agora = datetime.now(timezone.utc)
    linhas = [
        "# HELP dominio_dias_para_vencer Dias até o domínio expirar (RDAP), negativo se já venceu.",
        "# TYPE dominio_dias_para_vencer gauge",
        "# HELP dominio_rdap_ultima_consulta_timestamp Quando a expiração foi confirmada por último (epoch, segundos).",
        "# TYPE dominio_rdap_ultima_consulta_timestamp gauge",
    ]
    with cache_lock:
        instantaneo = dict(cache)
    for dominio in DOMINIOS:
        par = instantaneo.get(dominio)
        if not par:
            continue  # ainda não conseguiu consultar nenhuma vez — melhor ausente do que um zero errado
        expiracao, consultado_em = par
        dias = (expiracao - agora).total_seconds() / 86400
        linhas.append(f'dominio_dias_para_vencer{{dominio="{dominio}"}} {dias:.2f}')
        linhas.append(f'dominio_rdap_ultima_consulta_timestamp{{dominio="{dominio}"}} {consultado_em.timestamp():.0f}')
    return "\n".join(linhas) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        corpo = montar_metricas().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, format, *args):
        pass  # scrape a cada 15s no log seria só ruído; erro real já vai pro stderr em atualizar_cache


if __name__ == "__main__":
    threading.Thread(target=laco_refresh, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORTA), Handler).serve_forever()
