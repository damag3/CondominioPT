# -*- coding: utf-8 -*-
"""
Condomínio PT — gestão de condomínios em regime de propriedade horizontal (Portugal).
Só usa a biblioteca padrão do Python (tkinter + sqlite3). Dados em ~/CondominioPT.

Base legal usada nos valores por omissão (todos editáveis em "Condomínio"):
  - Fundo comum de reserva obrigatório, mínimo 10 % da quota ordinária (DL 268/94, art. 4.º, redação da Lei 8/2022)
  - Seguro contra risco de incêndio obrigatório (Código Civil, art. 1429.º)
  - Juros de mora civis 4 % ao ano (Portaria 291/2003, art. 559.º CC) — juro simples, dias/365
  - Convocatória com 10 dias de antecedência (CC, art. 1432.º); email permitido (Lei 8/2022)
  - Declaração de dívida / não dívida em 10 dias (Lei 8/2022)
  - Ata que delibere as contribuições é título executivo (DL 268/94, art. 6.º)
  - Despesas rateadas pela permilagem (total = 1000 ‰)
"""
import os, re, csv, ssl, shutil, hashlib, sqlite3, smtplib, webbrowser, html as H
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace

APP = "Condomínio PT"
BASE = os.environ.get("CONDOMINIO_DIR") or os.path.join(os.path.expanduser("~"), "CondominioPT")
for _d in ("", "docs", "backups"):
    os.makedirs(os.path.join(BASE, _d), exist_ok=True)
DBFILE = os.path.join(BASE, "condominio.db")
MESES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
PERIODOS = {"Mensal": list(range(1, 13)), "Trimestral": [1, 4, 7, 10], "Semestral": [1, 7], "Anual": [1]}
CATEGORIAS = ["Limpeza", "Eletricidade (partes comuns)", "Água", "Elevador", "Seguro", "Administração",
              "Jardinagem", "Manutenção e reparações", "Desinfestação", "Extintores / segurança", "Outras"]
FUNDO_MIN = 10.0

DEF = {"nome": "Condomínio do Edifício", "nif": "", "morada": "", "iban": "", "iban_fundo": "",
       "administrador": "", "email_admin": "", "periodicidade": "Mensal", "dia_venc": "8",
       "pct_fundo": "10", "taxa_juros": "4", "antecedencia": "10", "saldo_inicial": "0",
       "fundo_inicial": "0", "seguradora": "", "apolice": "", "seguro_capital": "",
       "seguro_renovacao": "", "smtp_host": "", "smtp_port": "587", "smtp_user": ""}


# ------------------------------------------------------------------ utilidades
def r2(x):
    return round(float(x or 0) + (1e-9 if float(x or 0) >= 0 else -1e-9), 2)


def eur(x):
    return f"{float(x or 0):,.2f} €".replace(",", " ").replace(".", ",")


def pdate(s):
    s = (s or "").strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError("Data inválida: " + s + " (use DD/MM/AAAA)")


def fdate(s):
    try:
        return pdate(s).strftime("%d/%m/%Y")
    except Exception:
        return s or ""


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    dias = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(y, m, min(d.day, dias[m - 1]))


def nif_ok(n):
    n = re.sub(r"\s", "", n or "")
    if not re.fullmatch(r"\d{9}", n):
        return False
    if n[0] not in "1235689" and n[:2] not in ("45", "70", "71", "72", "74", "75", "77", "79", "90", "91", "98", "99"):
        return False
    c = 11 - sum(int(n[i]) * (9 - i) for i in range(8)) % 11
    return int(n[8]) == (0 if c >= 10 else c)


def iban_ok(s):
    s = re.sub(r"\s", "", s or "").upper()
    if not re.fullmatch(r"PT\d{23}", s):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def email_ok(s):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s or ""))


# ------------------------------------------------------------------ base de dados
class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS condominos(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, nif TEXT, email TEXT,
            telefone TEXT, morada TEXT, notas TEXT);
        CREATE TABLE IF NOT EXISTS fracoes(id INTEGER PRIMARY KEY, designacao TEXT NOT NULL UNIQUE, tipo TEXT,
            area REAL, permilagem REAL NOT NULL, condomino_id INTEGER REFERENCES condominos(id));
        CREATE TABLE IF NOT EXISTS orcamento(id INTEGER PRIMARY KEY, ano INTEGER, categoria TEXT, valor REAL);
        CREATE TABLE IF NOT EXISTS quotas(id INTEGER PRIMARY KEY, fracao_id INTEGER NOT NULL REFERENCES fracoes(id),
            ano INTEGER, mes INTEGER, tipo TEXT, descricao TEXT, valor REAL, vencimento TEXT, chave TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS pagamentos(id INTEGER PRIMARY KEY, fracao_id INTEGER NOT NULL REFERENCES fracoes(id),
            data TEXT, valor REAL, tipo TEXT DEFAULT 'quota', metodo TEXT, referencia TEXT, notas TEXT, recibo TEXT);
        CREATE TABLE IF NOT EXISTS fornecedores(id INTEGER PRIMARY KEY, nome TEXT NOT NULL, nif TEXT, servico TEXT,
            email TEXT, telefone TEXT, contrato_fim TEXT, notas TEXT);
        CREATE TABLE IF NOT EXISTS despesas(id INTEGER PRIMARY KEY, data TEXT, fornecedor_id INTEGER REFERENCES fornecedores(id),
            categoria TEXT, descricao TEXT, base REAL, iva REAL DEFAULT 0, total REAL, documento TEXT,
            origem TEXT DEFAULT 'ordinaria', fundo TEXT DEFAULT 'Não', pago TEXT DEFAULT 'Sim');
        CREATE TABLE IF NOT EXISTS assembleias(id INTEGER PRIMARY KEY, tipo TEXT, data TEXT, hora TEXT, local TEXT,
            ordem TEXT, convocada_em TEXT, realizada TEXT DEFAULT 'Não', ata TEXT);
        CREATE TABLE IF NOT EXISTS obrigacoes(id INTEGER PRIMARY KEY, descricao TEXT NOT NULL, meses INTEGER,
            ultima TEXT, proxima TEXT, responsavel TEXT, notas TEXT);
        CREATE TABLE IF NOT EXISTS ocorrencias(id INTEGER PRIMARY KEY, data TEXT, titulo TEXT NOT NULL, descricao TEXT,
            local TEXT, estado TEXT DEFAULT 'Aberta', prioridade TEXT DEFAULT 'Normal',
            fornecedor_id INTEGER REFERENCES fornecedores(id), custo REAL, resolvida_em TEXT);
        CREATE TABLE IF NOT EXISTS documentos(id INTEGER PRIMARY KEY, data TEXT, categoria TEXT, descricao TEXT, ficheiro TEXT);
        """)
        if "recibo" not in [r["name"] for r in self.q("PRAGMA table_info(pagamentos)")]:
            self.c.execute("ALTER TABLE pagamentos ADD COLUMN recibo TEXT")
        if not self.q("SELECT 1 FROM obrigacoes LIMIT 1"):
            for d, m in (("Renovação do seguro contra incêndio (obrigatório)", 12),
                         ("Assembleia ordinária anual (contas e orçamento)", 12),
                         ("Verificação do fundo comum de reserva (administrador)", 12),
                         ("Manutenção do elevador (contrato)", 1), ("Verificação de extintores", 12),
                         ("Limpeza de caleiras e coberturas", 12)):
                self.c.execute("INSERT INTO obrigacoes(descricao,meses,notas) VALUES(?,?,?)",
                               (d, m, "Confirmar a periodicidade legal/contratual aplicável ao edifício."))
            self.c.commit()

    def q(self, sql, a=()):
        return self.c.execute(sql, a).fetchall()

    def x(self, sql, a=()):
        cur = self.c.execute(sql, a)
        self.c.commit()
        return cur.lastrowid


D = DB(DBFILE)


def cfg(k):
    r = D.q("SELECT v FROM config WHERE k=?", (k,))
    return r[0]["v"] if r and r[0]["v"] is not None else DEF.get(k, "")


def cfgf(k):
    try:
        return float(str(cfg(k)).replace(",", "."))
    except ValueError:
        return 0.0


def set_cfg(k, v):
    D.x("INSERT INTO config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ------------------------------------------------------------------ motor financeiro
def alocar(fid, ate=None):
    """Imputa os pagamentos às quotas mais antigas (FIFO) e calcula juros de mora (juro simples, dias/365)."""
    ate = ate or date.today()
    taxa = cfgf("taxa_juros") / 100
    qs = [dict(r, pago=0.0, juros=0.0, al=[]) for r in
          D.q("SELECT * FROM quotas WHERE fracao_id=? ORDER BY vencimento,id", (fid,))]
    jpago = credito = 0.0
    for p in D.q("SELECT * FROM pagamentos WHERE fracao_id=? ORDER BY data,id", (fid,)):
        if p["tipo"] == "juros":
            jpago += p["valor"]
            continue
        resto, pd = p["valor"], pdate(p["data"])
        for q in qs:
            if resto < 0.005:
                break
            em = q["valor"] - q["pago"]
            if em < 0.005:
                continue
            a = min(em, resto)
            q["pago"] += a
            resto -= a
            q["juros"] += a * taxa * max(0, (pd - pdate(q["vencimento"])).days) / 365
            q["al"].append((p["data"], a))
        credito += resto
    for q in qs:
        em = q["valor"] - q["pago"]
        v = pdate(q["vencimento"])
        q["em_falta"] = r2(em) if em >= 0.005 else 0.0
        q["vencida"] = q["em_falta"] > 0 and v < ate
        if q["vencida"]:
            q["juros"] += q["em_falta"] * taxa * (ate - v).days / 365
        q["juros"] = r2(q["juros"])
    return qs, r2(jpago), r2(credito)


def resumo(fid, ate=None):
    qs, jp, cred = alocar(fid, ate)
    divida = r2(sum(q["em_falta"] for q in qs if q["vencida"]))
    juros = max(0.0, r2(sum(q["juros"] for q in qs) - jp))
    return dict(qs=qs, divida=divida, juros=juros, credito=cred, total=r2(divida + juros),
                mais_antiga=min([pdate(q["vencimento"]) for q in qs if q["vencida"]], default=None))


def fracoes_info():
    return D.q("SELECT f.*, c.nome, c.email, c.nif AS cnif, c.morada AS cmorada FROM fracoes f "
               "LEFT JOIN condominos c ON c.id=f.condomino_id ORDER BY f.designacao")


def permilagem_total():
    return r2(sum(r["permilagem"] for r in D.q("SELECT permilagem FROM fracoes")))


def receitas(ano=None):
    """Recebimentos imputados, por tipo de quota (e juros), no ano indicado (ou desde sempre)."""
    t = {"ordinaria": 0.0, "fundo": 0.0, "extra": 0.0, "juros": 0.0}
    for f in D.q("SELECT id FROM fracoes"):
        qs, _, _ = alocar(f["id"])
        for q in qs:
            for dt, a in q["al"]:
                if ano is None or pdate(dt).year == ano:
                    t[q["tipo"]] = t.get(q["tipo"], 0) + a
    for p in D.q("SELECT data,valor FROM pagamentos WHERE tipo='juros'"):
        if ano is None or pdate(p["data"]).year == ano:
            t["juros"] += p["valor"]
    return {k: r2(v) for k, v in t.items()}


def saldos():
    rec = receitas()
    dfundo = D.q("SELECT COALESCE(SUM(total),0) s FROM despesas WHERE fundo='Sim' AND pago='Sim'")[0]["s"]
    dord = D.q("SELECT COALESCE(SUM(total),0) s FROM despesas WHERE fundo<>'Sim' AND pago='Sim'")[0]["s"]
    return dict(fundo=r2(cfgf("fundo_inicial") + rec["fundo"] - dfundo),
                ordinaria=r2(cfgf("saldo_inicial") + rec["ordinaria"] + rec["extra"] + rec["juros"] - dord))


def gerar_quotas(ano):
    fr = D.q("SELECT * FROM fracoes")
    if not fr:
        raise ValueError("Não existem frações.")
    if abs(permilagem_total() - 1000) > 0.01:
        raise ValueError(f"A soma das permilagens é {permilagem_total()} ‰ e tem de ser 1000 ‰.")
    total = D.q("SELECT COALESCE(SUM(valor),0) s FROM orcamento WHERE ano=?", (ano,))[0]["s"]
    if total <= 0:
        raise ValueError(f"Não há orçamento para {ano}.")
    pct = cfgf("pct_fundo")
    if pct < FUNDO_MIN:
        raise ValueError(f"O fundo de reserva não pode ser inferior a {FUNDO_MIN:.0f} % da quota (DL 268/94, art. 4.º).")
    per = PERIODOS.get(cfg("periodicidade"), PERIODOS["Mensal"])
    dia = min(int(cfgf("dia_venc") or 8), 28)
    n = 0
    for f in fr:
        anual = r2(total * f["permilagem"] / 1000)
        parc = r2(anual / len(per))
        for i, m in enumerate(per):
            v = r2(anual - parc * (len(per) - 1)) if i == len(per) - 1 else parc
            ven = date(ano, m, dia).isoformat()
            for tipo, val, desc in (("ordinaria", v, f"Quota ordinária {ano} – {MESES[m - 1]}"),
                                    ("fundo", r2(v * pct / 100), f"Fundo comum de reserva {ano} – {MESES[m - 1]}")):
                cur = D.c.execute("INSERT OR IGNORE INTO quotas(fracao_id,ano,mes,tipo,descricao,valor,vencimento,chave) "
                                  "VALUES(?,?,?,?,?,?,?,?)", (f["id"], ano, m, tipo, desc, val, ven, f"{f['id']}-{ano}-{m}-{tipo}"))
                n += cur.rowcount
    D.c.commit()
    return n


def quota_extra(desc, total, venc):
    if abs(permilagem_total() - 1000) > 0.01:
        raise ValueError("A soma das permilagens tem de ser 1000 ‰.")
    fr = D.q("SELECT * FROM fracoes ORDER BY id")
    vals = [r2(total * f["permilagem"] / 1000) for f in fr]
    vals[0] = r2(vals[0] + (total - sum(vals)))
    tag = datetime.now().strftime("%Y%m%d%H%M%S%f")
    v = pdate(venc)
    for f, val in zip(fr, vals):
        D.c.execute("INSERT INTO quotas(fracao_id,ano,mes,tipo,descricao,valor,vencimento,chave) VALUES(?,?,?,?,?,?,?,?)",
                    (f["id"], v.year, v.month, "extra", desc, val, v.isoformat(), f"x{tag}-{f['id']}"))
    D.c.commit()
    return len(fr)


def alertas():
    L, hoje = [], date.today()
    fr = fracoes_info()
    if not fr:
        L.append(("info", "Comece por registar condóminos e frações (permilagem total = 1000 ‰)."))
    elif abs(permilagem_total() - 1000) > 0.01:
        L.append(("erro", f"Permilagens somam {permilagem_total()} ‰ — têm de somar 1000 ‰ (art. 1418.º CC)."))
    if cfgf("pct_fundo") < FUNDO_MIN:
        L.append(("erro", f"Contribuição para o fundo de reserva abaixo do mínimo legal de {FUNDO_MIN:.0f} %."))
    if not cfg("iban_fundo"):
        L.append(("aviso", "IBAN da conta do fundo comum de reserva não indicado (deve estar depositado em banco)."))
    sr = cfg("seguro_renovacao")
    if not sr:
        L.append(("erro", "Seguro contra risco de incêndio não registado (obrigatório — art. 1429.º CC)."))
    else:
        try:
            dd = (pdate(sr) - hoje).days
            if dd < 0:
                L.append(("erro", f"Seguro de incêndio expirou há {-dd} dias."))
            elif dd <= 60:
                L.append(("aviso", f"Seguro de incêndio renova em {dd} dias — verificar capital segurado (atualização anual)."))
        except ValueError:
            pass
    if saldos()["fundo"] < 0:
        L.append(("erro", "Saldo do fundo comum de reserva negativo."))
    if not D.q("SELECT 1 FROM orcamento WHERE ano=?", (hoje.year,)):
        L.append(("aviso", f"Sem orçamento para {hoje.year}."))
    elif not D.q("SELECT 1 FROM quotas WHERE ano=? AND tipo='ordinaria' LIMIT 1", (hoje.year,)):
        L.append(("aviso", f"Quotas de {hoje.year} ainda não geradas."))
    if not D.q("SELECT 1 FROM assembleias WHERE tipo='Ordinária' AND realizada='Sim' AND substr(data,1,4)=?", (str(hoje.year),)):
        L.append(("aviso", f"Ainda não há assembleia ordinária realizada em {hoje.year} (contas e orçamento)."))
    nd = [0, 0, 0.0]
    for f in fr:
        r = resumo(f["id"])
        if r["divida"] > 0:
            nd[0] += 1
            nd[2] += r["total"]
            if r["mais_antiga"] and (hoje - r["mais_antiga"]).days > 90:
                nd[1] += 1
    if nd[0]:
        L.append(("aviso", f"{nd[0]} fração(ões) em dívida, total com juros {eur(nd[2])}."))
    if nd[1]:
        L.append(("erro", f"{nd[1]} fração(ões) com dívida há mais de 90 dias — ponderar interpelação e ação executiva (a ata que fixa as quotas é título executivo, art. 6.º DL 268/94)."))
    sem = 0
    for o in D.q("SELECT * FROM obrigacoes"):
        if not o["proxima"]:
            sem += 1
            continue
        dd = (pdate(o["proxima"]) - hoje).days
        if dd < 0:
            L.append(("erro", f"Obrigação em atraso: {o['descricao']} (devia ser em {fdate(o['proxima'])})."))
        elif dd <= 30:
            L.append(("aviso", f"Obrigação a vencer em {dd} dias: {o['descricao']}."))
    if sem:
        L.append(("info", f"{sem} obrigação(ões) sem data definida (separador Obrigações)."))
    oc = D.q("SELECT * FROM ocorrencias WHERE estado<>'Resolvida'")
    if oc:
        urg = sum(1 for o in oc if o["prioridade"] == "Urgente")
        L.append(("aviso" if urg else "info", f"{len(oc)} ocorrência(s) por resolver" + (f", {urg} urgente(s)." if urg else ".")))
    for fo in D.q("SELECT nome,contrato_fim FROM fornecedores WHERE contrato_fim IS NOT NULL AND contrato_fim<>''"):
        dd = (pdate(fo["contrato_fim"]) - hoje).days
        if dd <= 60:
            L.append(("aviso", f"Contrato com {fo['nome']} {'termina em %d dias' % dd if dd >= 0 else 'terminou'}."))
    return L


def backup():
    dst = os.path.join(BASE, "backups", f"condominio_{date.today():%Y%m%d}.db")
    if not os.path.exists(dst):
        D.c.commit()
        b = sqlite3.connect(dst)
        D.c.backup(b)
        b.close()
    for f in sorted(os.listdir(os.path.join(BASE, "backups")))[:-30]:
        os.remove(os.path.join(BASE, "backups", f))
    return dst


def automacao():
    msgs, ano = [], date.today().year
    try:
        if D.q("SELECT 1 FROM orcamento WHERE ano=?", (ano,)) and \
                not D.q("SELECT 1 FROM quotas WHERE ano=? AND tipo='ordinaria' LIMIT 1", (ano,)):
            msgs.append(f"Geradas {gerar_quotas(ano)} quotas de {ano}.")
    except ValueError as e:
        msgs.append("Quotas não geradas: " + str(e))
    backup()
    msgs.append(f"{len(alertas())} alertas ativos. Cópia de segurança do dia efetuada.")
    return msgs


# ------------------------------------------------------------------ documentos (HTML imprimível / PDF pelo navegador)
CSS = ("body{font-family:Georgia,'Times New Roman',serif;max-width:820px;margin:2em auto;color:#111;line-height:1.45}"
       "table{border-collapse:collapse;width:100%;margin:.6em 0}th,td{border-bottom:1px solid #999;padding:4px 6px;text-align:left}"
       "td.n,th.n{text-align:right}h1{font-size:1.4em;border-bottom:2px solid #111;padding-bottom:.2em}h2{font-size:1.1em;margin-top:1.6em}"
       ".peq{font-size:.85em;color:#444}@media print{body{margin:0}}")


def esc(x):
    return H.escape(str(x if x is not None else ""))


def doc(titulo, corpo, nome):
    p = os.path.join(BASE, "docs", f"{nome}_{datetime.now():%Y%m%d_%H%M%S}.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset=utf-8><title>{esc(titulo)}</title><style>{CSS}</style>{corpo}")
    webbrowser.open("file:///" + p.replace("\\", "/"))
    return p


def cab():
    return (f"<p class=peq><b>{esc(cfg('nome'))}</b><br>{esc(cfg('morada'))}<br>NIF {esc(cfg('nif'))}"
            f" · Administração: {esc(cfg('administrador'))} {esc(cfg('email_admin'))}</p>")


def tab(heads, rows, num=()):
    h = "".join(f"<th{' class=n' if i in num else ''}>{esc(x)}</th>" for i, x in enumerate(heads))
    b = "".join("<tr>" + "".join(f"<td{' class=n' if i in num else ''}>{esc(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><tr>{h}</tr>{b}</table>"


def frac_row(fid):
    return D.q("SELECT f.*, c.nome, c.email, c.nif AS cnif FROM fracoes f LEFT JOIN condominos c ON c.id=f.condomino_id WHERE f.id=?", (fid,))[0]


def extrato_html(fid):
    f, r = frac_row(fid), resumo(fid)
    rows = [(fdate(q["vencimento"]), q["descricao"], eur(q["valor"]), eur(q["pago"]), eur(q["em_falta"]), eur(q["juros"])) for q in r["qs"]]
    pags = [(fdate(p["data"]), p["tipo"], p["metodo"] or "", eur(p["valor"])) for p in
            D.q("SELECT * FROM pagamentos WHERE fracao_id=? ORDER BY data", (fid,))]
    return (cab() + f"<h1>Extrato de conta corrente — Fração {esc(f['designacao'])}</h1><p>Condómino: {esc(f['nome'])} · "
            f"Permilagem {f['permilagem']} ‰ · Emitido em {date.today():%d/%m/%Y}</p>"
            + tab(["Vencimento", "Descrição", "Valor", "Pago", "Em falta", "Juros"], rows, (2, 3, 4, 5))
            + "<h2>Pagamentos</h2>" + tab(["Data", "Tipo", "Método", "Valor"], pags, (3,))
            + f"<p><b>Dívida vencida:</b> {eur(r['divida'])} · <b>Juros de mora ({cfg('taxa_juros')} %):</b> {eur(r['juros'])} · "
              f"<b>Total em dívida:</b> {eur(r['total'])} · Crédito: {eur(r['credito'])}</p>")


def declaracao_html(fid):
    f, r = frac_row(fid), resumo(fid)
    lin = [(q["descricao"], fdate(q["vencimento"]), eur(q["em_falta"])) for q in r["qs"] if q["vencida"]]
    if r["divida"] == 0 and r["juros"] == 0:
        t = "Declaração de não dívida"
        corpo = (f"<p>Para os devidos efeitos, declara-se que o condómino <b>{esc(f['nome'])}</b>, proprietário da fração "
                 f"<b>{esc(f['designacao'])}</b> do {esc(cfg('nome'))}, <b>não tem quotas ou encargos de condomínio em dívida</b> "
                 f"à data de {date.today():%d/%m/%Y}.</p>")
    else:
        t = "Declaração de dívida"
        corpo = (f"<p>Para os devidos efeitos, declara-se que o condómino <b>{esc(f['nome'])}</b>, proprietário da fração "
                 f"<b>{esc(f['designacao'])}</b>, tem em dívida, à data de {date.today():%d/%m/%Y}, os seguintes encargos de condomínio:</p>"
                 + tab(["Descrição", "Vencimento", "Valor em falta"], lin, (2,))
                 + f"<p>Juros de mora à taxa de {esc(cfg('taxa_juros'))} %: {eur(r['juros'])}. <b>Total: {eur(r['total'])}.</b></p>")
    corpo += (f"<p>Encargos de condomínio em vigor: quota ordinária anual e fundo comum de reserva ({esc(cfg('pct_fundo'))} %), "
              f"conforme orçamento aprovado.</p><p class=peq>Emitida pelo administrador no prazo máximo de 10 dias após o pedido.</p>"
              f"<br><br><p>O Administrador: ________________________ ({esc(cfg('administrador'))})</p>")
    return cab() + f"<h1>{t}</h1>" + corpo


def aviso_texto(fid):
    f, r = frac_row(fid), resumo(fid)
    lin = "\n".join(f" - {q['descricao']} (venc. {fdate(q['vencimento'])}): {eur(q['em_falta'])}" for q in r["qs"] if q["vencida"])
    corpo = (f"Exmo(a). Senhor(a) {f['nome'] or ''},\n\nVerifica-se que a fração {f['designacao']} do {cfg('nome')} tem em dívida:\n{lin}\n\n"
             f"Juros de mora à taxa legal de {cfg('taxa_juros')} %: {eur(r['juros'])}\nTOTAL: {eur(r['total'])}\n\n"
             f"Solicita-se a regularização no prazo de 10 dias por transferência para o IBAN {cfg('iban') or '(indicar IBAN)'}, "
             f"indicando a fração.\n\nMais se informa que, nos termos do art. 6.º do DL 268/94, a ata da assembleia que delibere o montante das "
             f"contribuições constitui título executivo contra o proprietário em mora.\n\nCom os melhores cumprimentos,\n{cfg('administrador')}")
    return f"Aviso de quotas em atraso — Fração {f['designacao']}", corpo


def aviso_html(fid):
    a, c = aviso_texto(fid)
    return cab() + f"<h1>{esc(a)}</h1><p>{esc(c).replace(chr(10), '<br>')}</p>"


def convocatoria_texto(aid):
    a = D.q("SELECT * FROM assembleias WHERE id=?", (aid,))[0]
    pontos = "\n".join(f"  {i}. {l.strip()}" for i, l in enumerate([x for x in (a["ordem"] or "").splitlines() if x.strip()], 1))
    corpo = (f"Convocatória — Assembleia de Condóminos {a['tipo']}\n\nNos termos dos artigos 1431.º e 1432.º do Código Civil e do regulamento do condomínio, "
             f"convocam-se os condóminos do {cfg('nome')}, {cfg('morada')}, para a assembleia {str(a['tipo']).lower()} a realizar no dia "
             f"{fdate(a['data'])}, às {a['hora']}, em {a['local']}.\n\nOrdem de trabalhos:\n{pontos}\n\n"
             f"Se à hora marcada não estiver presente o quórum exigido, a assembleia reunirá em segunda convocatória, no mesmo local, "
             f"trinta minutos depois, deliberando nos termos da lei e do regulamento.\n\nOs condóminos podem fazer-se representar por procurador, "
             f"mediante carta dirigida ao administrador. Pede-se confirmação de receção deste email.\n\n"
             f"O Administrador, {cfg('administrador')}")
    return f"Convocatória — Assembleia {a['tipo']} de {fdate(a['data'])}", corpo, a


def convocatoria_html(aid):
    s, c, a = convocatoria_texto(aid)
    return cab() + "<h1>" + esc(c.split("\n")[0]) + "</h1><p>" + esc("\n".join(c.split("\n")[2:])).replace("\n", "<br>") + "</p>"


def minuta_ata_html(aid):
    a = D.q("SELECT * FROM assembleias WHERE id=?", (aid,))[0]
    rows = [(f["designacao"], f["nome"] or "", f["permilagem"], "☐", "") for f in fracoes_info()]
    pontos = "".join(f"<li>{esc(l)}</li>" for l in (a["ordem"] or "").splitlines() if l.strip())
    return (cab() + f"<h1>Ata da assembleia {esc(str(a['tipo']).lower())} — {fdate(a['data'])}</h1>"
            f"<p>Aos {fdate(a['data'])}, pelas {esc(a['hora'])}, reuniu em {esc(a['local'])} a assembleia de condóminos, "
            f"convocada em {fdate(a['convocada_em']) or '____'}, estando presentes condóminos representando ______ ‰ do valor total do prédio.</p>"
            "<h2>Presenças</h2>" + tab(["Fração", "Condómino", "‰", "Presente", "Assinatura / procurador"], rows, (2,))
            + f"<h2>Ordem de trabalhos e deliberações</h2><ol>{pontos}</ol><p>Deliberações: ______________________________________</p>"
              "<p class=peq>Em caso de deliberação sobre o montante das contribuições, a ata é título executivo (art. 6.º DL 268/94). "
              "A ata é assinada pelos condóminos presentes (art. 1432.º CC).</p>")


def mapa_quotas_html(ano):
    rows, tot = [], [0, 0, 0, 0]
    for f in fracoes_info():
        qs = [q for q in resumo(f["id"])["qs"] if q["ano"] == ano]
        v, p, e, j = (r2(sum(q[k] for q in qs)) for k in ("valor", "pago", "em_falta", "juros"))
        rows.append((f["designacao"], f["nome"] or "", f["permilagem"], eur(v), eur(p), eur(e), eur(j)))
        for i, x in enumerate((v, p, e, j)):
            tot[i] += x
    rows.append(("TOTAL", "", permilagem_total(), *[eur(x) for x in tot]))
    return cab() + f"<h1>Mapa de quotas {ano}</h1>" + tab(["Fração", "Condómino", "‰", "Quotas", "Pago", "Em falta", "Juros"], rows, (2, 3, 4, 5, 6))


def devedores_html():
    rows, tot = [], 0
    for f in fracoes_info():
        r = resumo(f["id"])
        if r["divida"] > 0:
            rows.append((f["designacao"], f["nome"] or "", fdate(r["mais_antiga"].isoformat()), eur(r["divida"]), eur(r["juros"]), eur(r["total"])))
            tot += r["total"]
    return cab() + f"<h1>Mapa de devedores em {date.today():%d/%m/%Y}</h1>" + tab(
        ["Fração", "Condómino", "Dívida desde", "Capital", "Juros", "Total"], rows, (3, 4, 5)) + f"<p><b>Total: {eur(tot)}</b></p>"


def contas_html(ano):
    rec, s = receitas(ano), saldos()
    desp = D.q("SELECT categoria, SUM(total) t FROM despesas WHERE substr(data,1,4)=? AND pago='Sim' GROUP BY categoria ORDER BY categoria", (str(ano),))
    orc = {r["categoria"]: r["valor"] for r in D.q("SELECT categoria, SUM(valor) valor FROM orcamento WHERE ano=? GROUP BY categoria", (ano,))}
    real = {r["categoria"]: r["t"] for r in D.q(
        "SELECT categoria, SUM(total) t FROM despesas WHERE substr(data,1,4)=? AND origem='ordinaria' GROUP BY categoria", (str(ano),))}
    cats = sorted(set(orc) | set(real))
    rows = [(c, eur(orc.get(c, 0)), eur(real.get(c, 0)), eur(real.get(c, 0) - orc.get(c, 0))) for c in cats]
    rows.append(("TOTAL", eur(sum(orc.values())), eur(sum(real.values())), eur(sum(real.values()) - sum(orc.values()))))
    td = sum(r["t"] or 0 for r in desp)
    return (cab() + f"<h1>Relatório de contas {ano}</h1><h2>Receitas recebidas no ano</h2>"
            + tab(["Rubrica", "Valor"], [("Quotas ordinárias", eur(rec["ordinaria"])), ("Fundo comum de reserva", eur(rec["fundo"])),
                                        ("Quotas extraordinárias", eur(rec["extra"])), ("Juros de mora", eur(rec["juros"])),
                                        ("TOTAL", eur(sum(rec.values())))], (1,))
            + "<h2>Despesas pagas por categoria</h2>" + tab(["Categoria", "Valor"], [(r["categoria"], eur(r["t"])) for r in desp] + [("TOTAL", eur(td))], (1,))
            + "<h2>Orçamento vs. execução (despesas ordinárias)</h2>" + tab(["Categoria", "Orçamento", "Executado", "Desvio"], rows, (1, 2, 3))
            + f"<h2>Saldos acumulados</h2><p>Conta do condomínio: <b>{eur(s['ordinaria'])}</b> · Fundo comum de reserva: <b>{eur(s['fundo'])}</b></p>"
            + devedores_html().split("</h1>", 1)[1])


def receitas_mensais(ano):
    m = [0.0] * 12
    for f in D.q("SELECT id FROM fracoes"):
        for q in alocar(f["id"])[0]:
            for dt, a in q["al"]:
                if pdate(dt).year == ano:
                    m[pdate(dt).month - 1] += a
    for p in D.q("SELECT data,valor FROM pagamentos WHERE tipo='juros'"):
        if pdate(p["data"]).year == ano:
            m[pdate(p["data"]).month - 1] += p["valor"]
    return [r2(x) for x in m]


def despesas_mensais(ano):
    m = [0.0] * 12
    for d in D.q("SELECT data,total FROM despesas WHERE pago='Sim' AND substr(data,1,4)=?", (str(ano),)):
        m[pdate(d["data"]).month - 1] += d["total"] or 0
    return [r2(x) for x in m]


def recibo_html(pid):
    p = D.q("SELECT * FROM pagamentos WHERE id=?", (pid,))[0]
    if not p["recibo"]:
        ano = pdate(p["data"]).year
        seqs = [int(r["recibo"].split("/")[1]) for r in D.q("SELECT recibo FROM pagamentos WHERE recibo LIKE ?", (f"R{ano}/%",))]
        D.x("UPDATE pagamentos SET recibo=? WHERE id=?", (f"R{ano}/{max(seqs, default=0) + 1:04d}", pid))
        p = D.q("SELECT * FROM pagamentos WHERE id=?", (pid,))[0]
    f = frac_row(p["fracao_id"])
    return (cab() + f"<h1>Recibo n.º {esc(p['recibo'])}</h1><p>Recebemos de <b>{esc(f['nome'])}</b>"
            f"{' (NIF ' + esc(f['cnif']) + ')' if f['cnif'] else ''}, proprietário da fração <b>{esc(f['designacao'])}</b>, "
            f"a quantia de <b>{eur(p['valor'])}</b>, em {fdate(p['data'])}, referente a "
            f"{'juros de mora' if p['tipo'] == 'juros' else 'quotas de condomínio'}.</p>"
            f"<p>Método: {esc(p['metodo'] or '—')} · Referência: {esc(p['referencia'] or '—')}</p><br>"
            f"<p>O Administrador: ________________________ ({esc(cfg('administrador'))})</p>"
            "<p class=peq>Documento interno do condomínio; não substitui fatura-recibo certificada quando legalmente exigida.</p>")


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 150000).hex()


def definir_pw(pw):
    if not pw:
        D.x("DELETE FROM config WHERE k IN ('pw_salt','pw_hash')")
        return
    salt = os.urandom(16).hex()
    set_cfg("pw_salt", salt)
    set_cfg("pw_hash", _hash(pw, salt))


def pw_ok(pw):
    return _hash(pw, cfg("pw_salt")) == cfg("pw_hash")


def abrir_path(p):
    os.startfile(p) if hasattr(os, "startfile") else webbrowser.open("file://" + p)


def enviar_email(destinos, pw):
    host, user = cfg("smtp_host"), cfg("smtp_user")
    if not host or not user:
        raise ValueError("Configure o servidor SMTP no separador Condomínio.")
    port = int(cfgf("smtp_port") or 587)
    ctx = ssl.create_default_context()
    srv = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
    with srv:
        if port != 465:
            srv.starttls(context=ctx)
        srv.login(user, pw)
        for d, ass, cor in destinos:
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = user, d, ass
            m.set_content(cor)
            srv.send_message(m)


# ------------------------------------------------------------------ interface gráfica
BG, CARD, INK, MUTED, LINE = "#f3f4f6", "#ffffff", "#1f2933", "#6b7280", "#e1e4e8"
ACCENT, SIDE, DANGER, WARN = "#1d4e5f", "#16212b", "#9b2c2c", "#8a5a00"
GRUPOS = [("", ["Painel"]),
          ("CADASTRO", ["Condomínio", "Condóminos", "Frações", "Fornecedores"]),
          ("FINANÇAS", ["Orçamento", "Quotas", "Mapa de quotas", "Conta corrente", "Pagamentos", "Despesas"]),
          ("GESTÃO", ["Assembleias", "Obrigações", "Ocorrências", "Arquivo"]),
          ("", ["Relatórios"])]


def F(sz=10, w="normal"):
    return ("Segoe UI", sz, w)


def main():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, simpledialog

    if cfg("pw_hash"):
        r = tk.Tk()
        r.withdraw()
        ok = False
        for _ in range(3):
            pw = simpledialog.askstring(APP, "Palavra-passe de acesso:", show="*", parent=r)
            if pw is None:
                break
            if pw_ok(pw):
                ok = True
                break
            messagebox.showerror(APP, "Palavra-passe incorreta.", parent=r)
        r.destroy()
        if not ok:
            return

    def estilo(root):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        root.configure(bg=BG)
        st.configure(".", background=BG, foreground=INK, font=F(10), fieldbackground=CARD, bordercolor=LINE, troughcolor=LINE)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=INK)
        st.configure("TButton", padding=(12, 6), background="#e5e8ec", relief="flat", borderwidth=0)
        st.map("TButton", background=[("active", "#d3d8de")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white")
        st.map("Accent.TButton", background=[("active", "#163d4b")], foreground=[("active", "white")])
        st.configure("Treeview", background=CARD, fieldbackground=CARD, rowheight=28, borderwidth=0, font=F(10))
        st.configure("Treeview.Heading", background="#e9edf0", foreground=INK, font=F(10, "bold"), relief="flat", padding=6)
        st.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "white")])
        st.layout("Plain.TNotebook.Tab", [])
        st.configure("Plain.TNotebook", borderwidth=0, tabmargins=0, background=BG)
        st.configure("TEntry", padding=5)
        st.configure("TCombobox", padding=5)

    class Form(tk.Toplevel):
        def __init__(s, app, titulo, campos, vals, ok):
            super().__init__(app.root)
            s.configure(bg=BG, padx=16, pady=12)
            s.title(titulo)
            s.transient(app.root)
            s.grab_set()
            s.campos, s.ok, s.w, s.map = campos, ok, {}, {}
            for i, (n, r, t, o) in enumerate(campos):
                ttk.Label(s, text=r).grid(row=i, column=0, sticky="ne", padx=8, pady=4)
                v = vals.get(n)
                if t == "m":
                    w = tk.Text(s, width=52, height=7, wrap="word", relief="solid", borderwidth=1, font=F(10))
                    w.insert("1.0", v or "")
                elif t == "f":
                    opts = [(None, "")] + [(x[0], str(x[1])) for x in D.q(o)]
                    s.map[n] = {lab: i_ for i_, lab in opts}
                    w = ttk.Combobox(s, width=50, state="readonly", values=[l for _, l in opts])
                    w.set(next((l for i_, l in opts if i_ == v), ""))
                elif t in ("c", "e"):
                    w = ttk.Combobox(s, width=50, state="readonly" if t == "c" else "normal", values=o)
                    w.set("" if v is None else (str(int(v)) if isinstance(v, float) and v == int(v) and n == "iva" else str(v)))
                else:
                    w = ttk.Entry(s, width=52)
                    w.insert(0, fdate(v) if t == "d" and v else ("" if v is None else str(v)))
                w.grid(row=i, column=1, padx=8, pady=4)
                s.w[n] = w
            b = ttk.Frame(s)
            b.grid(row=len(campos), column=0, columnspan=2, pady=(12, 0), sticky="e")
            ttk.Button(b, text="Cancelar", command=s.destroy).pack(side="right", padx=4)
            ttk.Button(b, text="Guardar", style="Accent.TButton", command=s.guardar).pack(side="right", padx=4)

        def valor(s, n, t):
            w = s.w[n]
            if t == "m":
                return w.get("1.0", "end").strip()
            x = w.get().strip()
            if t == "f":
                return s.map[n][x]
            if t == "n":
                try:
                    return float(x.replace(",", ".")) if x else None
                except ValueError:
                    raise ValueError(f"Número inválido: {x}")
            if t == "d":
                return pdate(x).isoformat() if x else None
            return x

        def guardar(s):
            d = {}
            try:
                for n, r, t, o in s.campos:
                    v = s.valor(n, t)
                    if r.endswith("*") and v in (None, ""):
                        raise ValueError("Preencha: " + r.rstrip("*"))
                    if v and n in ("nif", "cnif") and not nif_ok(v):
                        raise ValueError("NIF inválido (dígito de controlo incorreto).")
                    if v and n.startswith("iban") and not iban_ok(v):
                        raise ValueError("IBAN português inválido.")
                    if v and n == "email" and not email_ok(v):
                        raise ValueError("Email inválido.")
                    d[n] = v
                s.ok(d)
            except (ValueError, sqlite3.Error) as e:
                messagebox.showerror(APP, str(e), parent=s)
                return
            s.destroy()

    class Crud(ttk.Frame):
        def __init__(s, app, tabela, campos, cols, sql=None, extras=(), antes=None, rodape=None, topo=None, defs=None, novo_fn=None):
            super().__init__(app.nb)
            s.app, s.t, s.campos, s.cols, s.antes, s.rodape, s.defs, s.novo_fn = app, tabela, campos, cols, antes, rodape, defs or {}, novo_fn
            s.sql = sql or f"SELECT * FROM {tabela} ORDER BY id DESC"
            if topo:
                topo(s)
            bar = ttk.Frame(s)
            bar.pack(fill="x", pady=(2, 8))
            ttk.Button(bar, text="＋ Novo", style="Accent.TButton", command=s.novo).pack(side="left", padx=(0, 4))
            for txt, cmd in (("Editar", s.editar), ("Apagar", s.apagar)):
                ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=2)
            for txt, fn in extras:
                ttk.Button(bar, text=txt, command=lambda f=fn: f(s.sel())).pack(side="left", padx=2)
            s.q = tk.StringVar()
            ttk.Entry(bar, textvariable=s.q, width=24).pack(side="right")
            ttk.Label(bar, text="Pesquisar:").pack(side="right", padx=6)
            s.q.trace_add("write", lambda *a: s.refresh())
            fr = ttk.Frame(s)
            fr.pack(fill="both", expand=True)
            s.tv = ttk.Treeview(fr, columns=[c[0] for c in cols], show="headings", selectmode="browse")
            s.tv.tag_configure("odd", background="#f6f8fa")
            for k, t, w, ty in cols:
                s.tv.heading(k, text=t, anchor="e" if ty == "e" else "w")
                s.tv.column(k, width=w, anchor="e" if ty == "e" else "w")
            sb = ttk.Scrollbar(fr, command=s.tv.yview)
            s.tv.configure(yscrollcommand=sb.set)
            s.tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            s.lbl = ttk.Label(s, foreground=MUTED)
            s.lbl.pack(anchor="w", pady=6)
            s.tv.bind("<Double-1>", lambda e: s.editar())
            app.refs.append(s)

        def sel(s):
            x = s.tv.selection()
            return int(x[0]) if x else None

        def refresh(s):
            sql = s.sql() if callable(s.sql) else s.sql
            s.tv.delete(*s.tv.get_children())
            needle, n = s.q.get().lower().strip(), 0
            for r in D.q(sql):
                vals = []
                for k, t, w, ty in s.cols:
                    v = r[k]
                    vals.append(fdate(v) if ty == "d" else eur(v) if ty == "e" else "" if v is None else v)
                if needle and needle not in " ".join(map(str, vals)).lower():
                    continue
                s.tv.insert("", "end", iid=str(r["id"]), values=vals, tags=("odd",) if n % 2 else ())
                n += 1
            s.lbl.config(text=s.rodape() if s.rodape else f"{n} registo(s)")

        def novo(s):
            if s.novo_fn:
                return s.novo_fn()
            d = {k: (v() if callable(v) else v) for k, v in s.defs.items()}
            Form(s.app, "Novo registo", s.campos, d, lambda d: s.gravar(None, d))

        def editar(s):
            i = s.sel()
            if i is None:
                return messagebox.showinfo(APP, "Selecione uma linha.")
            Form(s.app, "Editar registo", s.campos, dict(D.q(f"SELECT * FROM {s.t} WHERE id=?", (i,))[0]), lambda d: s.gravar(i, d))

        def gravar(s, i, d):
            if s.antes:
                d = s.antes(d) or d
            cols = list(d.keys())
            if i:
                D.x(f"UPDATE {s.t} SET {','.join(c + '=?' for c in cols)} WHERE id=?", [d[c] for c in cols] + [i])
            else:
                D.x(f"INSERT INTO {s.t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols])
            s.app.refresh_all()

        def apagar(s):
            i = s.sel()
            if i is None or not messagebox.askyesno(APP, "Apagar o registo selecionado?"):
                return
            try:
                D.x(f"DELETE FROM {s.t} WHERE id=?", (i,))
            except sqlite3.IntegrityError:
                messagebox.showerror(APP, "Não é possível apagar: existem registos associados.")
            s.app.refresh_all()

    def anual(var):
        try:
            return int(var.get())
        except (ValueError, tk.TclError):
            return date.today().year

    class App:
        def __init__(s):
            s.root = tk.Tk()
            s.root.title(APP)
            s.root.geometry("1280x780")
            s.root.minsize(1000, 640)
            estilo(s.root)
            s.refs, s.nav = [], {}
            side = tk.Frame(s.root, bg=SIDE, width=230)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            tk.Label(side, text="Condomínio PT", bg=SIDE, fg="white", font=F(16, "bold")).pack(anchor="w", padx=20, pady=(22, 0))
            s.nome_l = tk.Label(side, bg=SIDE, fg="#8fa3b3", font=F(9), wraplength=190, justify="left")
            s.nome_l.pack(anchor="w", padx=20, pady=(2, 14))
            s.side = side
            right = ttk.Frame(s.root)
            right.pack(side="left", fill="both", expand=True, padx=22, pady=(16, 10))
            hd = ttk.Frame(right)
            hd.pack(fill="x")
            s.titulo = tk.Label(hd, bg=BG, fg=INK, font=F(20, "bold"))
            s.titulo.pack(side="left")
            tk.Label(hd, text=date.today().strftime("%d/%m/%Y"), bg=BG, fg=MUTED, font=F(10)).pack(side="right")
            tk.Frame(right, bg=LINE, height=1).pack(fill="x", pady=(8, 10))
            s.nb = ttk.Notebook(right, style="Plain.TNotebook")
            s.nb.pack(fill="both", expand=True)
            ano = date.today().year
            FR = "SELECT id, designacao FROM fracoes ORDER BY designacao"
            CO = "SELECT id, nome FROM condominos ORDER BY nome"
            FO = "SELECT id, nome FROM fornecedores ORDER BY nome"
            s.painel()
            s.cfg_tab()
            s.add(Crud(s, "condominos",
                       [("nome", "Nome*", "t", 0), ("nif", "NIF", "t", 0), ("email", "Email", "t", 0), ("telefone", "Telemóvel", "t", 0),
                        ("morada", "Morada", "t", 0), ("notas", "Notas", "m", 0)],
                       [("nome", "Nome", 260, "p"), ("nif", "NIF", 100, "p"), ("email", "Email", 220, "p"), ("telefone", "Telefone", 110, "p"), ("morada", "Morada", 300, "p")],
                       sql="SELECT * FROM condominos ORDER BY nome"), "Condóminos")
            s.add(Crud(s, "fracoes",
                       [("designacao", "Fração* (ex.: 1.º Esq.)", "t", 0), ("tipo", "Tipo", "e", ["Habitação", "Comércio", "Garagem", "Arrecadação", "Outro"]),
                        ("area", "Área (m²)", "n", 0), ("permilagem", "Permilagem (‰)*", "n", 0), ("condomino_id", "Proprietário", "f", CO)],
                       [("designacao", "Fração", 140, "p"), ("tipo", "Tipo", 110, "p"), ("area", "Área m²", 80, "p"), ("permilagem", "‰", 80, "p"), ("nome", "Proprietário", 280, "p")],
                       sql="SELECT f.*, c.nome FROM fracoes f LEFT JOIN condominos c ON c.id=f.condomino_id ORDER BY f.designacao",
                       rodape=lambda: f"Permilagem total: {permilagem_total()} ‰ (tem de ser 1000 ‰)"), "Frações")
            s.add(Crud(s, "fornecedores",
                       [("nome", "Nome*", "t", 0), ("nif", "NIF", "t", 0), ("servico", "Serviço", "t", 0), ("email", "Email", "t", 0), ("telefone", "Telefone", "t", 0),
                        ("contrato_fim", "Fim do contrato", "d", 0), ("notas", "Notas", "m", 0)],
                       [("nome", "Nome", 220, "p"), ("nif", "NIF", 100, "p"), ("servico", "Serviço", 200, "p"), ("telefone", "Telefone", 110, "p"), ("contrato_fim", "Fim contrato", 100, "d")],
                       sql="SELECT * FROM fornecedores ORDER BY nome"), "Fornecedores")
            s.orc = Crud(s, "orcamento",
                         [("ano", "Ano*", "n", 0), ("categoria", "Categoria*", "e", CATEGORIAS), ("valor", "Valor anual (€)*", "n", 0)],
                         [("ano", "Ano", 70, "p"), ("categoria", "Categoria", 300, "p"), ("valor", "Valor anual", 130, "e")],
                         sql=lambda: f"SELECT * FROM orcamento WHERE ano={anual(s.anov)} ORDER BY categoria",
                         antes=lambda d: dict(d, ano=int(d["ano"])), defs={"ano": lambda: anual(s.anov)}, topo=s.topo_orc,
                         rodape=lambda: f"Orçamento {anual(s.anov)}: {eur(D.q('SELECT COALESCE(SUM(valor),0) s FROM orcamento WHERE ano=?', (anual(s.anov),))[0]['s'])}"
                                        f" · fundo de reserva adicional: {cfg('pct_fundo')} %",
                         extras=[("Gerar quotas do ano", lambda i: s.gerar()), ("Copiar do ano anterior (+%)", lambda i: s.copiar_orc())])
            s.add(s.orc, "Orçamento")
            s.add(Crud(s, "quotas",
                       [("fracao_id", "Fração*", "f", FR), ("tipo", "Tipo*", "c", ["ordinaria", "fundo", "extra"]), ("descricao", "Descrição*", "t", 0),
                        ("valor", "Valor (€)*", "n", 0), ("vencimento", "Vencimento*", "d", 0)],
                       [("designacao", "Fração", 110, "p"), ("descricao", "Descrição", 330, "p"), ("tipo", "Tipo", 90, "p"), ("valor", "Valor", 100, "e"), ("vencimento", "Vencimento", 100, "d")],
                       sql="SELECT q.*, f.designacao FROM quotas q JOIN fracoes f ON f.id=q.fracao_id ORDER BY q.vencimento DESC, f.designacao",
                       antes=lambda d: dict(d, ano=pdate(d["vencimento"]).year, mes=pdate(d["vencimento"]).month, chave=f"m{datetime.now():%Y%m%d%H%M%S%f}"),
                       extras=[("Quota extraordinária (rateio)", lambda i: s.extra())]), "Quotas")
            s.mapa_tab()
            s.contas_tab()
            s.add(Crud(s, "pagamentos", s.camp_pag(),
                       [("designacao", "Fração", 100, "p"), ("data", "Data", 100, "d"), ("valor", "Valor", 100, "e"), ("tipo", "Tipo", 70, "p"), ("metodo", "Método", 120, "p"),
                        ("referencia", "Referência", 140, "p"), ("recibo", "Recibo", 100, "p")],
                       sql="SELECT p.*, f.designacao FROM pagamentos p JOIN fracoes f ON f.id=p.fracao_id ORDER BY p.data DESC, p.id DESC",
                       defs={"data": date.today().isoformat(), "tipo": "quota"},
                       extras=[("Emitir recibo", lambda i: i and (doc("Recibo", recibo_html(i), "recibo"), s.refresh_all()))]), "Pagamentos")
            s.add(Crud(s, "despesas",
                       [("data", "Data*", "d", 0), ("fornecedor_id", "Fornecedor", "f", FO), ("categoria", "Categoria*", "e", CATEGORIAS), ("descricao", "Descrição", "t", 0),
                        ("base", "Valor sem IVA (€)*", "n", 0), ("iva", "IVA (%)", "e", ["0", "6", "13", "23"]), ("documento", "N.º documento", "t", 0),
                        ("origem", "Origem", "c", ["ordinaria", "extraordinaria"]), ("fundo", "Paga pelo fundo de reserva?", "c", ["Não", "Sim"]), ("pago", "Pago?", "c", ["Sim", "Não"])],
                       [("data", "Data", 90, "d"), ("fnome", "Fornecedor", 190, "p"), ("categoria", "Categoria", 170, "p"), ("descricao", "Descrição", 230, "p"), ("total", "Total", 100, "e"), ("fundo", "Fundo?", 60, "p"), ("pago", "Pago?", 55, "p")],
                       sql="SELECT d.*, f.nome fnome FROM despesas d LEFT JOIN fornecedores f ON f.id=d.fornecedor_id ORDER BY d.data DESC",
                       antes=s.antes_desp, defs={"data": date.today().isoformat(), "iva": "23", "origem": "ordinaria", "fundo": "Não", "pago": "Sim"},
                       rodape=lambda: f"Total despesas {ano}: {eur(D.q('SELECT COALESCE(SUM(total),0) s FROM despesas WHERE substr(data,1,4)=?', (str(ano),))[0]['s'])}"), "Despesas")
            s.add(Crud(s, "assembleias",
                       [("tipo", "Tipo*", "c", ["Ordinária", "Extraordinária"]), ("data", "Data*", "d", 0), ("hora", "Hora*", "t", 0), ("local", "Local*", "t", 0),
                        ("ordem", "Ordem de trabalhos (1 ponto por linha)*", "m", 0), ("convocada_em", "Convocada em", "d", 0), ("realizada", "Realizada?", "c", ["Não", "Sim"]), ("ata", "Ata / deliberações", "m", 0)],
                       [("tipo", "Tipo", 110, "p"), ("data", "Data", 100, "d"), ("hora", "Hora", 60, "p"), ("local", "Local", 260, "p"), ("convocada_em", "Convocada", 100, "d"), ("realizada", "Realizada", 80, "p")],
                       sql="SELECT * FROM assembleias ORDER BY data DESC", defs={"tipo": "Ordinária", "hora": "21:00", "realizada": "Não"},
                       extras=[("Convocatória", s.convocatoria), ("Convocatória por email", s.email_conv), ("Minuta de ata", lambda i: i and doc("Ata", minuta_ata_html(i), "ata"))]), "Assembleias")
            s.add(Crud(s, "obrigacoes",
                       [("descricao", "Obrigação*", "t", 0), ("meses", "Periodicidade (meses)", "n", 0), ("ultima", "Última vez", "d", 0), ("proxima", "Próxima data", "d", 0),
                        ("responsavel", "Responsável", "t", 0), ("notas", "Notas", "m", 0)],
                       [("descricao", "Obrigação", 380, "p"), ("meses", "Meses", 60, "p"), ("ultima", "Última", 100, "d"), ("proxima", "Próxima", 100, "d"), ("responsavel", "Responsável", 160, "p")],
                       sql="SELECT * FROM obrigacoes ORDER BY CASE WHEN proxima IS NULL OR proxima='' THEN 1 ELSE 0 END, proxima",
                       antes=s.antes_obr, extras=[("Marcar como cumprida hoje", s.cumprida)]), "Obrigações")
            s.add(Crud(s, "ocorrencias",
                       [("data", "Data*", "d", 0), ("titulo", "Título*", "t", 0), ("descricao", "Descrição", "m", 0), ("local", "Local (ex.: escadas, garagem)", "t", 0),
                        ("estado", "Estado", "c", ["Aberta", "Em curso", "Resolvida"]), ("prioridade", "Prioridade", "c", ["Normal", "Urgente", "Baixa"]),
                        ("fornecedor_id", "Fornecedor", "f", FO), ("custo", "Custo (€)", "n", 0), ("resolvida_em", "Resolvida em", "d", 0)],
                       [("data", "Data", 90, "d"), ("titulo", "Ocorrência", 300, "p"), ("local", "Local", 150, "p"), ("prioridade", "Prioridade", 80, "p"),
                        ("estado", "Estado", 90, "p"), ("fnome", "Fornecedor", 160, "p"), ("custo", "Custo", 90, "e")],
                       sql="SELECT o.*, f.nome fnome FROM ocorrencias o LEFT JOIN fornecedores f ON f.id=o.fornecedor_id "
                           "ORDER BY CASE o.estado WHEN 'Resolvida' THEN 1 ELSE 0 END, o.data DESC",
                       defs={"data": date.today().isoformat(), "estado": "Aberta", "prioridade": "Normal"},
                       extras=[("Marcar resolvida", s.resolver)]), "Ocorrências")
            s.add(Crud(s, "documentos",
                       [("categoria", "Categoria*", "e", ["Ata", "Regulamento", "Seguro", "Contrato", "Fatura", "Orçamento de obra", "Certificado", "Outro"]), ("descricao", "Descrição*", "t", 0)],
                       [("data", "Arquivado em", 100, "d"), ("categoria", "Categoria", 140, "p"), ("descricao", "Descrição", 380, "p"), ("ficheiro", "Ficheiro", 360, "p")],
                       sql="SELECT * FROM documentos ORDER BY data DESC, id DESC", novo_fn=lambda: s.anexar(),
                       extras=[("Abrir ficheiro", lambda i: i and abrir_path(D.q("SELECT ficheiro FROM documentos WHERE id=?", (i,))[0]["ficheiro"]))]), "Arquivo")
            s.relatorios()
            s.menu()
            s.nb.bind("<<NotebookTabChanged>>", s.mudou)
            s.refresh_all()
            s.ir("Painel")
            s.root.protocol("WM_DELETE_WINDOW", s.sair)

        def add(s, w, t):
            s.nb.add(w, text=t)

        def menu(s):
            s.tabs = {s.nb.tab(t, "text"): t for t in s.nb.tabs()}
            for g, itens in GRUPOS:
                if g:
                    tk.Label(s.side, text=g, bg=SIDE, fg="#6f8596", font=F(8, "bold")).pack(anchor="w", padx=20, pady=(14, 2))
                for t in itens:
                    l = tk.Label(s.side, text="   " + t, bg=SIDE, fg="#cfd8e0", font=F(10), anchor="w", padx=8, pady=6, cursor="hand2")
                    l.pack(fill="x", padx=10, pady=1)
                    l.bind("<Button-1>", lambda e, t=t: s.ir(t))
                    l.bind("<Enter>", lambda e, l=l, t=t: l.config(bg="#22323f") if s.atual != t else None)
                    l.bind("<Leave>", lambda e, l=l, t=t: l.config(bg=SIDE) if s.atual != t else None)
                    s.nav[t] = l

        def ir(s, t):
            s.nb.select(s.tabs[t])

        def mudou(s, e=None):
            s.atual = s.nb.tab(s.nb.select(), "text")
            for t, l in s.nav.items():
                l.config(bg=ACCENT if t == s.atual else SIDE, fg="white" if t == s.atual else "#cfd8e0")
            s.titulo.config(text=s.atual)
            s.refresh_all()

        def refresh_all(s):
            s.nome_l.config(text=cfg("nome"))
            for r in s.refs:
                r.refresh()

        def sair(s):
            try:
                backup()
            finally:
                s.root.destroy()

        # ---- painel
        def painel(s):
            f = ttk.Frame(s.nb)
            s.nb.add(f, text="Painel")
            cards = ttk.Frame(f)
            cards.pack(fill="x")
            s.kp = {}
            for i, (k, t) in enumerate([("div", "Dívida vencida"), ("jur", "Juros de mora"), ("cta", "Conta do condomínio"),
                                        ("fun", "Fundo comum de reserva"), ("rec", "Receitas do ano"), ("dsp", "Despesas do ano")]):
                c = tk.Frame(cards, bg=CARD, highlightbackground=LINE, highlightthickness=1)
                c.grid(row=i // 3, column=i % 3, sticky="nsew", padx=(0 if i % 3 == 0 else 8, 0), pady=(0, 8))
                cards.columnconfigure(i % 3, weight=1, uniform="k")
                tk.Label(c, text=t.upper(), bg=CARD, fg=MUTED, font=F(8, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
                v = tk.Label(c, text="–", bg=CARD, fg=INK, font=F(19, "bold"))
                v.pack(anchor="w", padx=16, pady=(0, 12))
                s.kp[k] = v
            mid = ttk.Frame(f)
            mid.pack(fill="both", expand=True, pady=(4, 0))
            mid.columnconfigure(0, weight=3, uniform="m")
            mid.columnconfigure(1, weight=2, uniform="m")
            mid.rowconfigure(0, weight=1)
            s.cv = tk.Canvas(mid, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            s.cv.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            s.cv.bind("<Configure>", lambda e: s.desenhar())
            s.dados = ([0.0] * 12, [0.0] * 12)
            box = tk.Frame(mid, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            box.grid(row=0, column=1, sticky="nsew")
            tk.Label(box, text="ALERTAS E PRAZOS", bg=CARD, fg=MUTED, font=F(8, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
            s.al = tk.Text(box, wrap="word", state="disabled", relief="flat", bg=CARD, padx=16, pady=2, font=F(10), highlightthickness=0, cursor="arrow")
            s.al.pack(fill="both", expand=True)
            s.al.tag_configure("erro", foreground=DANGER)
            s.al.tag_configure("aviso", foreground=WARN)
            s.al.tag_configure("info", foreground=MUTED)
            b = ttk.Frame(f)
            b.pack(fill="x", pady=(8, 0))
            ttk.Button(b, text="Executar automação", style="Accent.TButton",
                       command=lambda: (messagebox.showinfo(APP, "\n".join(automacao())), s.refresh_all())).pack(side="left")
            ttk.Button(b, text="Cópia de segurança agora", command=lambda: messagebox.showinfo(APP, "Cópia em:\n" + backup())).pack(side="left", padx=6)
            ttk.Button(b, text="Abrir pasta de dados", command=lambda: abrir_path(BASE)).pack(side="left")
            s.refs.append(SimpleNamespace(refresh=s.painel_refresh))

        def desenhar(s):
            c = s.cv
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
            if w < 80 or h < 80:
                return
            rec, dsp = s.dados
            mx = max(rec + dsp + [1])
            pl, pb, pt = 52, 30, 62
            c.create_text(16, 20, anchor="w", text=f"Receitas e despesas {date.today().year}", font=F(11, "bold"), fill=INK)
            c.create_rectangle(16, 34, 26, 44, fill=ACCENT, width=0)
            c.create_text(32, 39, anchor="w", text="Receitas", font=F(9), fill=MUTED)
            c.create_rectangle(100, 34, 110, 44, fill="#a7b1bc", width=0)
            c.create_text(116, 39, anchor="w", text="Despesas", font=F(9), fill=MUTED)
            bw = (w - pl - 14) / 12
            for k in (0, 0.5, 1):
                y = h - pb - (h - pb - pt) * k
                c.create_line(pl, y, w - 10, y, fill=LINE)
                c.create_text(pl - 8, y, anchor="e", text=f"{mx * k:,.0f}".replace(",", " "), fill=MUTED, font=F(8))
            for i in range(12):
                x0 = pl + i * bw + bw * 0.14
                for j, (v, cor) in enumerate(((rec[i], ACCENT), (dsp[i], "#a7b1bc"))):
                    hh = (h - pb - pt) * v / mx
                    if hh < 1:
                        continue
                    c.create_rectangle(x0 + j * bw * 0.36, h - pb - hh, x0 + j * bw * 0.36 + bw * 0.34, h - pb, fill=cor, width=0)
                c.create_text(pl + i * bw + bw / 2, h - pb + 14, text=MESES[i], fill=MUTED, font=F(8))

        def painel_refresh(s):
            sd, ano = saldos(), date.today().year
            dv = jr = 0.0
            for f in D.q("SELECT id FROM fracoes"):
                r = resumo(f["id"])
                dv += r["divida"]
                jr += r["juros"]
            rc = receitas(ano)
            dp = D.q("SELECT COALESCE(SUM(total),0) s FROM despesas WHERE substr(data,1,4)=? AND pago='Sim'", (str(ano),))[0]["s"]
            for k, v in (("div", dv), ("jur", jr), ("cta", sd["ordinaria"]), ("fun", sd["fundo"]), ("rec", sum(rc.values())), ("dsp", dp)):
                s.kp[k].config(text=eur(v), fg=DANGER if (k == "div" and v > 0) or (k in ("cta", "fun") and v < 0) else INK)
            s.dados = (receitas_mensais(ano), despesas_mensais(ano))
            s.desenhar()
            s.al.config(state="normal")
            s.al.delete("1.0", "end")
            sim = {"erro": "✖  ", "aviso": "▲  ", "info": "ℹ  "}
            L = alertas()
            for n, t in sorted(L, key=lambda x: ["erro", "aviso", "info"].index(x[0])):
                s.al.insert("end", sim[n] + t + "\n\n", n)
            if not L:
                s.al.insert("end", "Sem alertas. Tudo em dia.", "info")
            s.al.config(state="disabled")

        # ---- configuração
        def cfg_tab(s):
            f = ttk.Frame(s.nb)
            s.nb.add(f, text="Condomínio")
            camp = [("nome", "Nome do condomínio"), ("nif", "NIF"), ("morada", "Morada"), ("administrador", "Administrador"), ("email_admin", "Email do administrador"),
                    ("iban", "IBAN conta do condomínio"), ("iban_fundo", "IBAN conta do fundo de reserva"), ("periodicidade", "Periodicidade das quotas"),
                    ("dia_venc", "Dia de vencimento (1-28)"), ("pct_fundo", "Fundo de reserva (% da quota, mín. 10)"), ("taxa_juros", "Juros de mora (% anual)"),
                    ("antecedencia", "Antecedência da convocatória (dias)"), ("saldo_inicial", "Saldo inicial da conta (€)"), ("fundo_inicial", "Saldo inicial do fundo (€)"),
                    ("seguradora", "Seguradora"), ("apolice", "N.º da apólice"), ("seguro_capital", "Capital segurado (€)"), ("seguro_renovacao", "Renovação do seguro (DD/MM/AAAA)"),
                    ("smtp_host", "SMTP – servidor"), ("smtp_port", "SMTP – porta (587/465)"), ("smtp_user", "SMTP – utilizador/email")]
            s.v = {}
            for i, (k, r) in enumerate(camp):
                ttk.Label(f, text=r).grid(row=i % 11, column=(i // 11) * 2, sticky="e", padx=8, pady=5)
                w = ttk.Combobox(f, values=list(PERIODOS), state="readonly", width=36) if k == "periodicidade" else ttk.Entry(f, width=40)
                w.grid(row=i % 11, column=(i // 11) * 2 + 1, padx=6, pady=5, sticky="w")
                s.v[k] = w
            b = ttk.Frame(f)
            b.grid(row=12, column=0, columnspan=4, pady=14, padx=8, sticky="w")
            ttk.Button(b, text="Guardar configuração", style="Accent.TButton", command=s.guardar_cfg).pack(side="left")
            ttk.Button(b, text="Palavra-passe de acesso…", command=s.mudar_pw).pack(side="left", padx=8)
            ttk.Label(f, text="A palavra-passe do email é pedida no envio e nunca é guardada. A palavra-passe de acesso bloqueia a abertura do programa "
                              "mas não cifra o ficheiro de dados.", foreground=MUTED, wraplength=900).grid(row=13, column=0, columnspan=4, padx=8, sticky="w")
            s.refs.append(SimpleNamespace(refresh=s.cfg_refresh))

        def cfg_refresh(s):
            for k, w in s.v.items():
                v = fdate(cfg(k)) if k == "seguro_renovacao" else cfg(k)
                if isinstance(w, ttk.Combobox):
                    w.set(v)
                else:
                    w.delete(0, "end")
                    w.insert(0, v)

        def mudar_pw(s):
            a = simpledialog.askstring(APP, "Nova palavra-passe de acesso (vazio = remover):", show="*")
            if a is None:
                return
            if a and simpledialog.askstring(APP, "Repita a palavra-passe:", show="*") != a:
                return messagebox.showerror(APP, "As palavras-passe não coincidem.")
            definir_pw(a)
            messagebox.showinfo(APP, "Palavra-passe definida." if a else "Palavra-passe removida.")

        def guardar_cfg(s):
            try:
                d = {k: w.get().strip() for k, w in s.v.items()}
                if d["nif"] and not nif_ok(d["nif"]):
                    raise ValueError("NIF inválido.")
                for k in ("iban", "iban_fundo"):
                    if d[k] and not iban_ok(d[k]):
                        raise ValueError("IBAN inválido: " + k)
                if d["email_admin"] and not email_ok(d["email_admin"]):
                    raise ValueError("Email do administrador inválido.")
                if float(d["pct_fundo"].replace(",", ".")) < FUNDO_MIN:
                    raise ValueError("O fundo comum de reserva não pode ser inferior a 10 % (DL 268/94, art. 4.º).")
                if d["seguro_renovacao"]:
                    d["seguro_renovacao"] = pdate(d["seguro_renovacao"]).isoformat()
                for k in ("pct_fundo", "taxa_juros", "saldo_inicial", "fundo_inicial", "dia_venc", "antecedencia"):
                    d[k] = d[k].replace(",", ".")
                    float(d[k] or 0)
            except ValueError as e:
                return messagebox.showerror(APP, str(e) if ("inválid" in str(e) or "10 %" in str(e)) else "Valor numérico inválido.")
            for k, v in d.items():
                set_cfg(k, v)
            s.refresh_all()
            messagebox.showinfo(APP, "Configuração guardada.")

        # ---- orçamento / quotas
        def topo_orc(s, crud):
            t = ttk.Frame(crud)
            t.pack(fill="x", pady=(0, 6))
            ttk.Label(t, text="Ano:").pack(side="left")
            s.anov = tk.IntVar(value=date.today().year)
            sp = ttk.Spinbox(t, from_=2000, to=2100, textvariable=s.anov, width=7, command=crud.refresh)
            sp.pack(side="left", padx=6)
            sp.bind("<Return>", lambda e: crud.refresh())

        def gerar(s):
            try:
                n = gerar_quotas(anual(s.anov))
                messagebox.showinfo(APP, f"{n} quotas criadas (as já existentes não são duplicadas).")
            except ValueError as e:
                messagebox.showerror(APP, str(e))
            s.refresh_all()

        def copiar_orc(s):
            a = anual(s.anov)
            p = simpledialog.askfloat(APP, f"Copiar orçamento de {a - 1} para {a}.\nAtualização em % (ex.: 2,3):", initialvalue=0.0)
            if p is None:
                return
            if D.q("SELECT 1 FROM orcamento WHERE ano=?", (a,)) and not messagebox.askyesno(APP, "Já existe orçamento neste ano. Acrescentar mesmo assim?"):
                return
            for r in D.q("SELECT categoria,valor FROM orcamento WHERE ano=?", (a - 1,)):
                D.c.execute("INSERT INTO orcamento(ano,categoria,valor) VALUES(?,?,?)", (a, r["categoria"], r2(r["valor"] * (1 + p / 100))))
            D.c.commit()
            s.refresh_all()

        def extra(s):
            d = simpledialog.askstring(APP, "Descrição da quota extraordinária (ex.: Pintura da fachada):")
            if not d:
                return
            v = simpledialog.askfloat(APP, "Valor TOTAL a ratear pela permilagem (€):", minvalue=0.01)
            ven = simpledialog.askstring(APP, "Data de vencimento (DD/MM/AAAA):", initialvalue=(date.today() + timedelta(days=30)).strftime("%d/%m/%Y"))
            if not v or not ven:
                return
            try:
                n = quota_extra(d, v, ven)
                messagebox.showinfo(APP, f"Criadas {n} quotas. Lembre-se: a quota extraordinária deve ter sido deliberada em assembleia.")
            except ValueError as e:
                messagebox.showerror(APP, str(e))
            s.refresh_all()

        def mapa_tab(s):
            f = ttk.Frame(s.nb)
            s.nb.add(f, text="Mapa de quotas")
            t = ttk.Frame(f)
            t.pack(fill="x", pady=(0, 8))
            ttk.Label(t, text="Ano:").pack(side="left")
            s.am = tk.IntVar(value=date.today().year)
            sp = ttk.Spinbox(t, from_=2000, to=2100, textvariable=s.am, width=7, command=s.mapa_refresh)
            sp.pack(side="left", padx=6)
            sp.bind("<Return>", lambda e: s.mapa_refresh())
            ttk.Label(t, text="✓ pago     valor = em falta (€)     · a vencer     linhas a vermelho = devedores", foreground=MUTED).pack(side="left", padx=16)
            cols = ["fr"] + MESES + ["tot"]
            s.mp = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
            s.mp.tag_configure("dev", foreground=DANGER)
            s.mp.tag_configure("odd", background="#f6f8fa")
            s.mp.heading("fr", text="Fração", anchor="w")
            s.mp.column("fr", width=130, anchor="w")
            for m in MESES:
                s.mp.heading(m, text=m)
                s.mp.column(m, width=62, anchor="center")
            s.mp.heading("tot", text="Em falta", anchor="e")
            s.mp.column("tot", width=110, anchor="e")
            s.mp.pack(fill="both", expand=True)
            s.refs.append(SimpleNamespace(refresh=s.mapa_refresh))

        def mapa_refresh(s):
            ano = anual(s.am)
            s.mp.delete(*s.mp.get_children())
            for n, f in enumerate(fracoes_info()):
                r, cel, tot = resumo(f["id"]), [], 0.0
                for m in range(1, 13):
                    qs = [q for q in r["qs"] if q["ano"] == ano and q["mes"] == m]
                    if not qs:
                        cel.append("")
                        continue
                    ef = r2(sum(q["em_falta"] for q in qs))
                    if ef == 0:
                        cel.append("✓")
                    elif any(q["vencida"] for q in qs):
                        cel.append(f"{ef:.0f}")
                        tot += ef
                    else:
                        cel.append("·")
                s.mp.insert("", "end", values=(f["designacao"], *cel, eur(tot) if tot else "—"), tags=("dev",) if tot > 0 else (("odd",) if n % 2 else ()))

        # ---- conta corrente
        def camp_pag(s):
            return [("fracao_id", "Fração*", "f", "SELECT id, designacao FROM fracoes ORDER BY designacao"), ("data", "Data*", "d", 0), ("valor", "Valor (€)*", "n", 0),
                    ("tipo", "Tipo", "c", ["quota", "juros"]), ("metodo", "Método", "e", ["Transferência", "Multibanco", "Numerário", "Cheque", "Débito direto"]),
                    ("referencia", "Referência", "t", 0), ("notas", "Notas", "m", 0)]

        def contas_tab(s):
            f = ttk.Frame(s.nb)
            s.nb.add(f, text="Conta corrente")
            t = ttk.Frame(f)
            t.pack(fill="x", pady=(0, 8))
            s.cb = ttk.Combobox(t, state="readonly", width=40)
            s.cb.pack(side="left", padx=(0, 8))
            s.cb.bind("<<ComboboxSelected>>", lambda e: s.cc_refresh())
            ttk.Button(t, text="Registar pagamento", style="Accent.TButton", command=s.cc_pagar).pack(side="left", padx=2)
            for txt, fn in (("Extrato", lambda: s.cc_doc(extrato_html, "extrato")), ("Declaração", lambda: s.cc_doc(declaracao_html, "declaracao")),
                            ("Aviso de atraso", lambda: s.cc_doc(aviso_html, "aviso")), ("Aviso por email", s.cc_email)):
                ttk.Button(t, text=txt, command=fn).pack(side="left", padx=2)
            cols = ("v", "d", "val", "pg", "ef", "j")
            s.cc = ttk.Treeview(f, columns=cols, show="headings")
            s.cc.tag_configure("dev", foreground=DANGER)
            s.cc.tag_configure("ok", foreground=MUTED)
            for c, h, w in zip(cols, ("Vencimento", "Descrição", "Valor", "Pago", "Em falta", "Juros"), (100, 380, 100, 100, 100, 100)):
                s.cc.heading(c, text=h, anchor="w" if c in ("v", "d") else "e")
                s.cc.column(c, width=w, anchor="w" if c in ("v", "d") else "e")
            s.cc.pack(fill="both", expand=True)
            s.cc_l = ttk.Label(f, font=F(10, "bold"))
            s.cc_l.pack(anchor="w", pady=8)
            s.refs.append(SimpleNamespace(refresh=s.cc_refresh))

        def cc_id(s):
            x = s.cb.get()
            return int(x.split(" – ")[0]) if x else None

        def cc_refresh(s):
            cur = s.cb.get()
            vals = [f"{f['id']} – {f['designacao']} ({f['nome'] or 'sem proprietário'})" for f in fracoes_info()]
            s.cb["values"] = vals
            if cur not in vals:
                s.cb.set(vals[0] if vals else "")
            s.cc.delete(*s.cc.get_children())
            i = s.cc_id()
            if not i:
                return s.cc_l.config(text="")
            r = resumo(i)
            for q in r["qs"]:
                s.cc.insert("", "end", values=(fdate(q["vencimento"]), q["descricao"], eur(q["valor"]), eur(q["pago"]), eur(q["em_falta"]), eur(q["juros"])),
                            tags=("dev",) if q["vencida"] else (("ok",) if q["em_falta"] == 0 else ()))
            s.cc_l.config(text=f"Dívida vencida {eur(r['divida'])}   ·   Juros {eur(r['juros'])}   ·   TOTAL {eur(r['total'])}   ·   Crédito {eur(r['credito'])}")

        def cc_pagar(s):
            i = s.cc_id()
            if i:
                Form(s, "Registar pagamento", s.camp_pag(), {"fracao_id": i, "data": date.today().isoformat(), "tipo": "quota"}, s.grava_pag)

        def grava_pag(s, d):
            c = list(d)
            D.x(f"INSERT INTO pagamentos({','.join(c)}) VALUES({','.join('?' * len(c))})", [d[k] for k in c])
            s.refresh_all()

        def cc_doc(s, fn, nome):
            i = s.cc_id()
            if i:
                doc(nome, fn(i), nome)

        def cc_email(s):
            i = s.cc_id()
            if not i:
                return
            f = frac_row(i)
            if not f["email"]:
                return messagebox.showinfo(APP, "O condómino não tem email registado.")
            s.enviar([(f["email"],) + aviso_texto(i)])

        def enviar(s, lista):
            if not messagebox.askyesno(APP, f"Enviar {len(lista)} email(s) a partir de {cfg('smtp_user') or '(SMTP por configurar)'}?"):
                return
            pw = simpledialog.askstring(APP, "Palavra-passe do email (não é guardada):", show="*")
            if not pw:
                return
            try:
                enviar_email(lista, pw)
                messagebox.showinfo(APP, "Enviado.")
            except Exception as e:
                messagebox.showerror(APP, f"Falha no envio: {e}")

        # ---- ações específicas
        def antes_desp(s, d):
            iva = float(str(d.get("iva") or 0).replace(",", "."))
            d["iva"], d["total"] = iva, r2((d["base"] or 0) * (1 + iva / 100))
            if d.get("fundo") == "Sim" and not messagebox.askyesno(
                    APP, "O fundo comum de reserva destina-se a despesas de conservação do edifício.\nUsá-lo para outro fim exige deliberação da assembleia. Confirma?"):
                raise ValueError("Operação cancelada.")
            return d

        def antes_obr(s, d):
            if d.get("meses") and d.get("ultima") and not d.get("proxima"):
                d["proxima"] = add_months(pdate(d["ultima"]), int(d["meses"])).isoformat()
            return d

        def cumprida(s, i):
            if not i:
                return
            o = D.q("SELECT * FROM obrigacoes WHERE id=?", (i,))[0]
            hoje = date.today()
            prox = add_months(hoje, int(o["meses"])).isoformat() if o["meses"] else None
            D.x("UPDATE obrigacoes SET ultima=?, proxima=? WHERE id=?", (hoje.isoformat(), prox, i))
            s.refresh_all()

        def resolver(s, i):
            if i:
                D.x("UPDATE ocorrencias SET estado='Resolvida', resolvida_em=? WHERE id=?", (date.today().isoformat(), i))
                s.refresh_all()

        def anexar(s):
            p = filedialog.askopenfilename(title="Escolha o documento a arquivar")
            if p:
                Form(s, "Arquivar documento", [("categoria", "Categoria*", "e", ["Ata", "Regulamento", "Seguro", "Contrato", "Fatura", "Orçamento de obra", "Certificado", "Outro"]),
                                               ("descricao", "Descrição*", "t", 0)], {"categoria": "Outro", "descricao": os.path.basename(p)}, lambda d: s.arquivar(p, d))

        def arquivar(s, p, d):
            pasta = os.path.join(BASE, "arquivo")
            os.makedirs(pasta, exist_ok=True)
            dst = os.path.join(pasta, f"{datetime.now():%Y%m%d%H%M%S}_{os.path.basename(p)}")
            shutil.copy2(p, dst)
            D.x("INSERT INTO documentos(data,categoria,descricao,ficheiro) VALUES(?,?,?,?)", (date.today().isoformat(), d["categoria"], d["descricao"], dst))
            s.refresh_all()

        def convocatoria(s, i):
            if not i:
                return
            a = D.q("SELECT * FROM assembleias WHERE id=?", (i,))[0]
            base = pdate(a["convocada_em"]) if a["convocada_em"] else date.today()
            dias = (pdate(a["data"]) - base).days
            n = int(cfgf("antecedencia") or 10)
            if dias < n and not messagebox.askyesno(APP, f"Atenção: só há {dias} dias entre a convocatória e a assembleia (mínimo legal {n}).\nGerar mesmo assim?"):
                return
            doc("Convocatória", convocatoria_html(i), "convocatoria")

        def email_conv(s, i):
            if not i:
                return
            ass, cor, _ = convocatoria_texto(i)
            lst = [(f["email"], ass, cor) for f in fracoes_info() if f["email"]]
            if not lst:
                return messagebox.showinfo(APP, "Nenhum condómino tem email registado.")
            s.enviar(lst)

        # ---- relatórios
        def relatorios(s):
            f = ttk.Frame(s.nb)
            s.nb.add(f, text="Relatórios")
            t = ttk.Frame(f)
            t.pack(anchor="w", pady=(0, 10))
            ttk.Label(t, text="Ano:").pack(side="left")
            s.ar = tk.IntVar(value=date.today().year)
            ttk.Spinbox(t, from_=2000, to=2100, textvariable=s.ar, width=7).pack(side="left", padx=6)
            bt = [("Relatório de contas (para a assembleia)", lambda: doc("Contas", contas_html(anual(s.ar)), "contas")),
                  ("Mapa de quotas do ano", lambda: doc("Mapa", mapa_quotas_html(anual(s.ar)), "mapa_quotas")),
                  ("Mapa de devedores", lambda: doc("Devedores", devedores_html(), "devedores")),
                  ("Avisos de atraso por email (todos os devedores)", s.avisos_massa),
                  ("Exportar quotas (CSV)", lambda: s.exportar("quotas", "SELECT q.vencimento, f.designacao, q.tipo, q.descricao, q.valor FROM quotas q JOIN fracoes f ON f.id=q.fracao_id ORDER BY q.vencimento")),
                  ("Exportar pagamentos (CSV)", lambda: s.exportar("pagamentos", "SELECT p.data, f.designacao, p.tipo, p.valor, p.metodo, p.referencia, p.recibo FROM pagamentos p JOIN fracoes f ON f.id=p.fracao_id ORDER BY p.data")),
                  ("Exportar despesas (CSV)", lambda: s.exportar("despesas", "SELECT data, categoria, descricao, base, iva, total, documento, origem, fundo FROM despesas ORDER BY data"))]
            for txt, fn in bt:
                ttk.Button(f, text=txt, command=fn, width=52).pack(anchor="w", pady=3)
            ttk.Label(f, text="Os documentos abrem no navegador: Imprimir → Guardar como PDF.", foreground=MUTED).pack(anchor="w", pady=12)

        def exportar(s, nome, sql):
            p = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=nome + ".csv", filetypes=[("CSV", "*.csv")])
            if not p:
                return
            rows = D.q(sql)
            with open(p, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, delimiter=";")
                if rows:
                    w.writerow(rows[0].keys())
                    w.writerows([[str(c).replace(".", ",") if isinstance(c, float) else c for c in r] for r in rows])
            messagebox.showinfo(APP, "Exportado para:\n" + p)

        def avisos_massa(s):
            lst = [(f["email"],) + aviso_texto(f["id"]) for f in fracoes_info() if f["email"] and resumo(f["id"])["divida"] > 0]
            if not lst:
                return messagebox.showinfo(APP, "Não há devedores com email registado.")
            s.enviar(lst)

    automacao()
    a = App()
    a.root.mainloop()


if __name__ == "__main__":
    main()
