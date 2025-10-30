
import os
import sqlite3
from datetime import datetime, date
from flask import Flask, request, redirect, url_for,make_response, Response, render_template, flash, session, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import pdfkit
import logging

APP_SECRET = os.getenv("APP_SECRET", "super-secret")
DB_PATH = os.path.join("db", "gerir_contas.db")

app = Flask(__name__)
app.secret_key = APP_SECRET

# ---------------------- Helpers DB ----------------------

def get_conn():
    os.makedirs("db", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            senha TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            status TEXT DEFAULT 'ativo'
        );
        """
    )

    # accounts (duas contas apenas: Poupança/BCI e Despesas/BIM)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            banco TEXT,
            tipo TEXT CHECK(tipo IN ('poupanca','despesas')) NOT NULL,
            saldo REAL DEFAULT 0,
            UNIQUE(user_id, tipo),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # transactions (regista tudo: entradas/saídas e transf.)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            tipo TEXT CHECK(tipo IN ('income','expense')) NOT NULL,
            valor REAL NOT NULL,
            descricao TEXT,
            categoria TEXT,
            pair_id INTEGER, -- para ligar lançamentos de transferências (saida+entrada)
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        );
        """
    )

    # debts (controlo de dívidas)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            valor_total REAL NOT NULL,
            valor_pago REAL DEFAULT 0,
            due_date TEXT,
            status TEXT DEFAULT 'pendente',
            notas TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    conn.commit()

    # seed user admin
    cur.execute("SELECT id FROM users WHERE email=?", ("admin@demo.mz",))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (nome, email, senha, role, status) VALUES (?,?,?,?,?)",
            ("Admin", "admin@demo.mz", generate_password_hash("1234"), "admin", "ativo"),
        )
        conn.commit()
        user_id = cur.lastrowid
    else:
        user_id = row["id"]

    # seed 2 contas fixas
    for nome, banco, tipo in [
        ("Poupança", "BCI", "poupanca"),
        ("Despesas", "BIM", "despesas"),
    ]:
        try:
            cur.execute(
                "INSERT INTO accounts (user_id, nome, banco, tipo, saldo) VALUES (?,?,?,?,0)",
                (user_id, nome, banco, tipo),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass

    conn.close()


# ---------------------- Auth ----------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        senha = request.form.get("senha")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=?", (email,))
        user = cur.fetchone()
        conn.close()
        if user and user["status"] == "ativo" and check_password_hash(user["senha"], senha):
            session["user_id"] = user["id"]
            session["nome"] = user["nome"]
            return redirect(url_for("dashboard"))
        flash("Credenciais inválidas ou utilizador inativo.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão terminada.", "info")
    return redirect(url_for("login"))


# ---------------------- Util ----------------------

def require_login(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def user_accounts(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM accounts WHERE user_id=? ORDER BY tipo", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def recalc_balances(user_id):
    """Recalcula saldos a partir das transações."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM accounts WHERE user_id=?", (user_id,))
    accs = [r["id"] for r in cur.fetchall()]
    for acc_id in accs:
        cur.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo='income' THEN valor ELSE -valor END),0) FROM transactions WHERE user_id=? AND account_id=?",
            (user_id, acc_id),
        )
        saldo = cur.fetchone()[0] or 0
        cur.execute("UPDATE accounts SET saldo=? WHERE id=?", (saldo, acc_id))
    conn.commit()
    conn.close()


# ---------------------- Dashboard ----------------------
@app.route("/")
@app.route("/dashboard")
@require_login
def dashboard():
    user_id = session["user_id"]
    contas = user_accounts(user_id)

    conn = get_conn()
    cur = conn.cursor()

    hoje = date.today()
    first_month = hoje.replace(day=1)

    # === KPIs mensais (EXCLUINDO transferências internas) ===
    cur.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN tipo='income'  THEN valor END),0) as total_in,
          COALESCE(SUM(CASE WHEN tipo='expense' THEN valor END),0) as total_out
        FROM transactions
        WHERE user_id=?
          AND date(data) >= date(?)
          AND (categoria IS NULL OR categoria <> 'transfer')
        """,
        (user_id, first_month.isoformat()),
    )
    agg = cur.fetchone()
    total_in = float(agg["total_in"] or 0)
    total_out = float(agg["total_out"] or 0)
    net_month = total_in - total_out
    savings_rate = (net_month / total_in * 100.0) if total_in > 0 else 0.0

    # Média diária de gastos no mês
    dias_passados = hoje.day
    avg_daily_spend = (total_out / max(dias_passados, 1))

    # Dívidas em aberto
    cur.execute(
        "SELECT COALESCE(SUM(valor_total - valor_pago),0) AS aberto FROM debts WHERE user_id=? AND status='pendente'",
        (user_id,),
    )
    divida_aberta = float(cur.fetchone()["aberto"] or 0)

    # Série 30 dias (EXCLUINDO transfer)
    cur.execute(
        """
        SELECT strftime('%Y-%m-%d', data) d,
               SUM(CASE WHEN tipo='income'  THEN valor ELSE 0 END) as inc,
               SUM(CASE WHEN tipo='expense' THEN valor ELSE 0 END) as exp
        FROM transactions
        WHERE user_id=?
          AND date(data) >= date('now','-29 day')
          AND (categoria IS NULL OR categoria <> 'transfer')
        GROUP BY d ORDER BY d
        """,
        (user_id,),
    )
    series = cur.fetchall()
    labels = [r["d"] for r in series]
    incs = [float(r["inc"] or 0) for r in series]
    exps = [float(r["exp"] or 0) for r in series]

    # Despesas por categoria (mês) – EXCLUINDO transfer
    cur.execute(
        """
        SELECT COALESCE(categoria,'(sem categoria)') as cat, SUM(valor) as total
        FROM transactions
        WHERE user_id=?
          AND tipo='expense'
          AND date(data) >= date(?)
          AND (categoria IS NULL OR categoria <> 'transfer')
        GROUP BY categoria
        ORDER BY total DESC
        """,
        (user_id, first_month.isoformat()),
    )
    exp_rows = cur.fetchall()
    exp_cats = [r["cat"] for r in exp_rows]
    exp_vals = [float(r["total"] or 0) for r in exp_rows]
    top_exp_cat = f"{exp_cats[0]} — {exp_vals[0]:.2f} MT" if exp_rows else "—"

    # Saldos por conta (barra)
    saldo_labels = [f"{c['nome']} ({c['banco']})" for c in contas]
    saldo_vals = [float(c["saldo"] or 0) for c in contas]

    conn.close()

    return render_template(
        "dashboard.html",
        contas=contas,
        total_in=total_in,
        total_out=total_out,
        divida_aberta=divida_aberta,
        labels=labels,
        incs=incs,
        exps=exps,
        net_month=net_month,
        savings_rate=savings_rate,
        avg_daily_spend=avg_daily_spend,
        top_exp_cat=top_exp_cat,
        saldo_labels=saldo_labels,
        saldo_vals=saldo_vals,
        exp_cats=exp_cats,
        exp_vals=exp_vals,
    )



# ---------------------- Transações (LISTA + FILTROS + CARDS) ----------------------
@app.route("/transactions", methods=["GET"])
@require_login
def transactions():
    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    # --------- Filtros ----------
    where = ["t.user_id = ?"]
    params = [user_id]

    q_from = request.args.get("from")
    q_to = request.args.get("to")
    q_tipo = request.args.get("tipo")
    q_acc = request.args.get("account_id", type=int)
    q_cat = request.args.get("categoria")
    q_text = request.args.get("q")

    if q_from:
        where.append("date(t.data) >= date(?)")
        params.append(q_from)
    if q_to:
        where.append("date(t.data) <= date(?)")
        params.append(q_to)
    if q_tipo in ("income", "expense"):
        where.append("t.tipo = ?")
        params.append(q_tipo)
    if q_acc:
        where.append("t.account_id = ?")
        params.append(q_acc)
    if q_cat:
        where.append("COALESCE(t.categoria,'') LIKE ?")
        params.append(f"%{q_cat}%")
    if q_text:
        where.append("(COALESCE(t.descricao,'') LIKE ? OR COALESCE(a.nome,'') LIKE ?)")
        params.extend([f"%{q_text}%", f"%{q_text}%"])

    where_sql = " AND ".join(where)

    # --------- Tabela ----------
    cur.execute(
        f"""
        SELECT t.id, t.data, t.tipo, t.valor, t.descricao, t.categoria,
               a.nome as conta, a.tipo as tipo_conta
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE {where_sql}
        ORDER BY date(t.data) DESC, t.id DESC
        LIMIT 2000
        """,
        params,
    )
    rows = cur.fetchall()

    # --------- KPIs do período (exclui transfer) ----------
    cur.execute(
        f"""
        SELECT
          COALESCE(SUM(CASE WHEN t.tipo='income'  THEN t.valor END),0) as total_in,
          COALESCE(SUM(CASE WHEN t.tipo='expense' THEN t.valor END),0) as total_out
        FROM transactions t
        WHERE {where_sql}
          AND (t.categoria IS NULL OR LOWER(t.categoria) <> 'transfer')
        """,
        params,
    )
    kpi = cur.fetchone()
    kpi_in = float(kpi["total_in"] or 0)
    kpi_out = float(kpi["total_out"] or 0)

    # --------- Saldos por conta ----------
    contas = user_accounts(user_id)  # pode retornar sqlite3.Row
    # normaliza para dict para permitir .get()
    contas = [dict(c) if not isinstance(c, dict) else c for c in contas]

    saldo_poupanca = 0.0
    saldo_despesas = 0.0
    for c in contas:
        nome = (c.get("nome") or "").lower()
        tipo = (c.get("tipo") or "").lower()
        saldo = float(c.get("saldo") or 0)
        if tipo == "poupanca" or "poup" in nome:
            saldo_poupanca = saldo
        if tipo == "despesas" or "desp" in nome:
            saldo_despesas = saldo

    # --------- Totais gerais do histórico (exclui transfer) ----------
    cur.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN tipo='income'  THEN valor END),0) as total_in_all,
          COALESCE(SUM(CASE WHEN tipo='expense' THEN valor END),0) as total_out_all
        FROM transactions
        WHERE user_id=?
          AND (categoria IS NULL OR LOWER(categoria) <> 'transfer')
        """,
        (user_id,),
    )
    tot = cur.fetchone()
    total_in_all = float(tot["total_in_all"] or 0)
    total_out_all = float(tot["total_out_all"] or 0)

    conn.close()

    return render_template(
        "transactions.html",
        rows=rows,
        contas=contas,
        kpi_in=kpi_in,
        kpi_out=kpi_out,
        saldo_poupanca=saldo_poupanca,
        saldo_despesas=saldo_despesas,
        total_in_all=total_in_all,
        total_out_all=total_out_all,
    )


# ---------------------- Transações (NOVO) ----------------------
@app.route("/transactions/new", methods=["GET", "POST"])
@require_login
def transactions_new():
    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    if request.method == "POST":
        try:
            tipo = request.form.get("tipo")  # income / expense
            account_id = int(request.form.get("account_id"))
            data_str = request.form.get("data") or date.today().isoformat()
            valor = float(request.form.get("valor", 0))
            descricao = request.form.get("descricao")
            categoria = request.form.get("categoria")

            # validação básica
            if tipo not in ("income", "expense"):
                raise ValueError("Tipo inválido.")
            if valor <= 0:
                raise ValueError("Valor tem que ser maior que zero.")

            # inserir no banco
            cur.execute(
                """
                INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria)
                VALUES (?,?,?,?,?,?,?)
                """,
                (user_id, account_id, data_str, tipo, valor, descricao, categoria),
            )
            conn.commit()

            # atualizar saldos das contas
            recalc_balances(user_id)

            # manda mensagem para o próximo GET
            flash("Movimento registado com sucesso ✅", "success")

        except Exception as e:
            conn.rollback()
            flash(f"Erro ao registar movimento: {e}", "danger")

        # MUITO IMPORTANTE: redirect depois de flash
        conn.close()
        return redirect(url_for("transactions_new"))

    # Se for GET normal (ou seja, carregar a página ou depois do redirect)
    contas_rows = user_accounts(user_id)
    contas = [dict(c) if not isinstance(c, dict) else c for c in contas_rows]

    conn.close()
    return render_template(
        "transactions_new.html",
        contas=contas
    )
# ---------------------- Transações (EXPORT CSV) ----------------------
@app.route("/transactions/export")
@require_login
def transactions_export():
    import csv
    from io import StringIO

    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    # mesmos filtros da lista
    where = ["t.user_id = ?"]
    params = [user_id]
    q_from = request.args.get("from")
    q_to = request.args.get("to")
    q_tipo = request.args.get("tipo")
    q_acc = request.args.get("account_id")
    q_cat = request.args.get("categoria")
    q_text = request.args.get("q")

    if q_from:
        where.append("date(t.data) >= date(?)")
        params.append(q_from)
    if q_to:
        where.append("date(t.data) <= date(?)")
        params.append(q_to)
    if q_tipo in ("income", "expense"):
        where.append("t.tipo = ?")
        params.append(q_tipo)
    if q_acc:
        where.append("t.account_id = ?")
        params.append(int(q_acc))
    if q_cat:
        where.append("COALESCE(t.categoria,'') LIKE ?")
        params.append(f"%{q_cat}%")
    if q_text:
        where.append("(COALESCE(t.descricao,'') LIKE ? OR COALESCE(a.nome,'') LIKE ?)")
        params.extend([f"%{q_text}%", f"%{q_text}%"])

    where_sql = " AND ".join(where)

    cur.execute(
        f"""
        SELECT t.data, a.nome as conta, t.tipo, t.valor, t.descricao, t.categoria
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        WHERE {where_sql}
        ORDER BY date(t.data) DESC, t.id DESC
        """,
        params,
    )
    rows = cur.fetchall()
    conn.close()

    # CSV
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow(["Data", "Conta", "Tipo", "Valor", "Descrição", "Categoria"])
    for r in rows:
        writer.writerow(
            [
                r["data"],
                r["conta"],
                r["tipo"],
                f"{r['valor']:.2f}",
                r["descricao"] or "",
                r["categoria"] or "",
            ]
        )
    out = si.getvalue()

    from flask import Response

    return Response(
        out,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=movimentos.csv"},
    )


# Transferências (para split mensal, por ex. do salário da conta BIM->BCI)
@app.route("/transfer", methods=["POST"])
@require_login
def transfer():
    user_id = session["user_id"]
    from_acc = int(request.form.get("from_account"))
    to_acc = int(request.form.get("to_account"))
    data_str = request.form.get("data") or date.today().isoformat()
    valor = float(request.form.get("valor"))
    descricao = request.form.get("descricao") or "Transferência"

    conn = get_conn()
    cur = conn.cursor()
    # cria par: expense numa conta + income na outra, ligados por pair_id
    cur.execute(
        "INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria) VALUES (?,?,?,?,?,?,?)",
        (user_id, from_acc, data_str, "expense", valor, descricao, "transfer"),
    )
    pair_id = cur.lastrowid
    cur.execute(
        "INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria, pair_id) VALUES (?,?,?,?,?,?,?,?)",
        (user_id, to_acc, data_str, "income", valor, descricao, "transfer", pair_id),
    )
    cur.execute("UPDATE transactions SET pair_id=? WHERE id=?", (pair_id, pair_id))
    conn.commit()
    conn.close()
    recalc_balances(user_id)
    flash("Transferência concluída.", "success")
    return redirect(url_for("transactions"))


# Split rápido do salário do dia 1 (percentagens)
@app.route("/salary_split", methods=["POST"])
@require_login
def salary_split():
    user_id = session["user_id"]
    total = float(request.form.get("valor_total"))
    pct_poup = float(request.form.get("pct_poupanca"))  # ex: 40 => 40%
    data_str = request.form.get("data") or date.today().isoformat()

    # obter ids das contas
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM accounts WHERE user_id=? AND tipo='despesas'", (user_id,))
    acc_desp = cur.fetchone()["id"]
    cur.execute("SELECT id FROM accounts WHERE user_id=? AND tipo='poupanca'", (user_id,))
    acc_poup = cur.fetchone()["id"]

    # entrada do salário na conta de despesas (BIM)
    cur.execute(
        "INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria) VALUES (?,?,?,?,?,?,?)",
        (user_id, acc_desp, data_str, "income", total, "Salário mensal", "salario"),
    )
    conn.commit()

    # transferência da percentagem para poupança
    valor_poup = round(total * (pct_poup / 100.0), 2)
    if valor_poup > 0:
        # expense em despesas
        cur.execute(
            "INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria) VALUES (?,?,?,?,?,?,?)",
            (user_id, acc_desp, data_str, "expense", valor_poup, "Transferência poupança", "transfer"),
        )
        pair_id = cur.lastrowid
        # income em poupança
        cur.execute(
            "INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria, pair_id) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, acc_poup, data_str, "income", valor_poup, "Transferência poupança", "transfer", pair_id),
        )
        cur.execute("UPDATE transactions SET pair_id=? WHERE id=?", (pair_id, pair_id))
        conn.commit()

    conn.close()
    recalc_balances(user_id)
    flash("Salário registado e dividido.", "success")
    return redirect(url_for("transactions"))


# ---------------------- Dívidas ----------------------
# ---------------------- Dívidas (LISTAR / CRIAR) ----------------------
# ---------------------- Dívidas (lista + criar) ----------------------
@app.route("/debts", methods=["GET", "POST"])
@require_login
def debts():
    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    success_msg = None
    error_msg = None

    # Criar nova dívida
    if request.method == "POST":
        try:
            nome = request.form.get("nome")
            valor_total = float(request.form.get("valor_total", 0))
            due_date = request.form.get("due_date") or None
            notas = request.form.get("notas")

            if not nome or valor_total <= 0:
                raise ValueError("Preenche o nome e um valor > 0")

            cur.execute("""
                INSERT INTO debts (user_id, nome, valor_total, valor_pago, due_date, status, notas)
                VALUES (?, ?, ?, 0, ?, 'pendente', ?)
            """, (user_id, nome, valor_total, due_date, notas))
            conn.commit()

            flash("Dívida registada com sucesso ✅", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Erro ao registar dívida: {e}", "danger")

        return redirect(url_for("debts"))

    # Listar dívidas
    cur.execute("""
        SELECT id, nome, valor_total, valor_pago, due_date, status, notas
        FROM debts
        WHERE user_id=?
        ORDER BY
          CASE status WHEN 'pendente' THEN 0 ELSE 1 END,
          date(due_date) IS NULL,
          date(due_date) ASC
    """, (user_id,))
    rows = cur.fetchall()

    # Também vamos precisar das contas para saber se há contas antes de pagar
    contas_rows = user_accounts(user_id)
    contas = [dict(c) if not isinstance(c, dict) else c for c in contas_rows]

    conn.close()

    return render_template(
        "debts.html",
        rows=rows,
        contas=contas,
    )

# ---------------------- Pagar dívida ----------------------
# ---------------------- Pagar dívida (form dedicado) ----------------------
@app.route("/debts/pay/<int:debt_id>", methods=["GET", "POST"])
@require_login
def pay_debt(debt_id):
    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    # Buscar dívida
    cur.execute("""
        SELECT id, nome, valor_total, valor_pago, due_date, status
        FROM debts
        WHERE user_id=? AND id=?
    """, (user_id, debt_id))
    debt = cur.fetchone()

    if not debt:
        conn.close()
        flash("Dívida não encontrada.", "danger")
        return redirect(url_for("debts"))

    aberto = float(debt["valor_total"] - debt["valor_pago"])

    # Buscar contas para escolher de onde sai o dinheiro
    contas_rows = user_accounts(user_id)
    contas = [dict(c) if not isinstance(c, dict) else c for c in contas_rows]

    if request.method == "POST":
        try:
            account_id = int(request.form.get("account_id"))
            data_str = request.form.get("data") or date.today().isoformat()
            valor = float(request.form.get("valor", 0))

            if valor <= 0:
                raise ValueError("Valor tem que ser maior que zero.")
            if valor > aberto:
                raise ValueError("Não podes pagar mais do que o valor em aberto.")

            # 1. Registar saída na tabela transactions
            cur.execute("""
                INSERT INTO transactions (user_id, account_id, data, tipo, valor, descricao, categoria)
                VALUES (?,?,?,?,?,?,?)
            """, (
                user_id,
                account_id,
                data_str,
                "expense",
                valor,
                f"Pagamento dívida: {debt['nome']}",
                "divida"
            ))

            # 2. Atualizar valor_pago na dívida
            cur.execute("""
                UPDATE debts
                SET valor_pago = valor_pago + ?
                WHERE id=? AND user_id=?
            """, (valor, debt_id, user_id))

            # 3. Ver se ficou 100% paga -> status = 'paga'
            cur.execute("""
                SELECT valor_total, valor_pago
                FROM debts
                WHERE id=? AND user_id=?
            """, (debt_id, user_id))
            check = cur.fetchone()
            if check and (check["valor_total"] - check["valor_pago"]) <= 0.005:
                cur.execute("""
                    UPDATE debts
                    SET status='paga'
                    WHERE id=? AND user_id=?
                """, (debt_id, user_id))

            # 4. Recalcular saldos das contas
            conn.commit()
            recalc_balances(user_id)

            flash("Pagamento registado com sucesso ✅", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Erro ao pagar dívida: {e}", "danger")

        conn.close()
        return redirect(url_for("debts"))

    # GET → mostrar formulário
    conn.close()
    return render_template(
        "debt_pay.html",
        debt=debt,
        aberto=aberto,
        contas=contas,
    )


# ---------------------- Relatório (helpers) ----------------------



def _render_report_html(print_mode=False):
    """Calcula dados e devolve HTML (string) já renderizado."""
    user_id = session["user_id"]
    conn = get_conn()
    cur = conn.cursor()

    # --- contas
    cur.execute("""
        SELECT id, nome, banco, tipo, saldo
        FROM accounts
        WHERE user_id=?
        ORDER BY tipo, nome
    """, (user_id,))
    contas_rows = cur.fetchall()
    contas = [dict(r) if not isinstance(r, dict) else r for r in contas_rows]

    # --- saldos agregados
    saldo_total = 0.0
    saldo_poupanca = 0.0
    saldo_despesas = 0.0

    for c in contas:
        saldo_c = float(c.get("saldo") or 0)
        saldo_total += saldo_c
        nome_c = (c.get("nome") or "").lower()
        tipo_c = (c.get("tipo") or "").lower()
        if tipo_c == "poupanca" or "poup" in nome_c:
            saldo_poupanca = saldo_c
        if tipo_c == "despesas" or "desp" in nome_c:
            saldo_despesas = saldo_c

    # --- totais históricos (sem transfer)
    cur.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN tipo='income'  THEN valor END),0) AS total_in,
          COALESCE(SUM(CASE WHEN tipo='expense' THEN valor END),0) AS total_out
        FROM transactions
        WHERE user_id=?
          AND (categoria IS NULL OR LOWER(categoria) <> 'transfer')
    """, (user_id,))
    t_all = cur.fetchone()
    total_in_all = float(t_all["total_in"] or 0)
    total_out_all = float(t_all["total_out"] or 0)

    # --- dívidas abertas
    cur.execute("""
        SELECT COALESCE(SUM(valor_total - valor_pago),0) AS aberto
        FROM debts
        WHERE user_id=? AND status='pendente'
    """, (user_id,))
    dividas_abertas = float(cur.fetchone()["aberto"] or 0)

    # --- período actual
    hoje = date.today()
    first_month_date = hoje.replace(day=1)
    first_month_iso = first_month_date.isoformat()  # '2025-10-01'
    periodo_label = f"{first_month_iso[8:10]}/{first_month_iso[5:7]}/{first_month_iso[0:4]} a {hoje.strftime('%d/%m/%Y')}"

    # --- totais do mês (sem transfer)
    cur.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN tipo='income'  THEN valor END),0) AS month_in,
          COALESCE(SUM(CASE WHEN tipo='expense' THEN valor END),0) AS month_out
        FROM transactions
        WHERE user_id=? AND date(data) >= date(?)
          AND (categoria IS NULL OR LOWER(categoria) <> 'transfer')
    """, (user_id, first_month_iso))
    m = cur.fetchone()
    month_in = float(m["month_in"] or 0)
    month_out = float(m["month_out"] or 0)

    # --- despesas por categoria (no mês)
    cur.execute("""
        SELECT LOWER(COALESCE(categoria,'(sem)')) AS cat, SUM(valor) AS total
        FROM transactions
        WHERE user_id=? AND date(data) >= date(?)
          AND tipo='expense'
          AND (categoria IS NULL OR LOWER(categoria) <> 'transfer')
        GROUP BY cat
        ORDER BY total DESC
    """, (user_id, first_month_iso))
    cat_rows = cur.fetchall()
    cat_expenses = [(r["cat"], float(r["total"] or 0)) for r in cat_rows]

    # --- últimos 60 movimentos
    cur.execute("""
        SELECT t.data, a.nome AS conta, t.tipo, t.valor, t.descricao, t.categoria
        FROM transactions t
        JOIN accounts a ON a.id=t.account_id
        WHERE t.user_id=?
        ORDER BY date(t.data) DESC, t.id DESC
        LIMIT 60
    """, (user_id,))
    rows = cur.fetchall()

    conn.close()

    patrimonio_liquido = saldo_total - dividas_abertas

    # qual template usar
    template_name = "report.html" if not print_mode else "report_pdf.html"

    html = render_template(
        template_name,
        contas=contas,
        saldo_poupanca=saldo_poupanca,
        saldo_despesas=saldo_despesas,
        saldo_total=saldo_total,
        patrimonio_liquido=patrimonio_liquido,
        total_in_all=total_in_all,
        total_out_all=total_out_all,
        dividas_abertas=dividas_abertas,
        month_in=month_in,
        month_out=month_out,
        cat_expenses=cat_expenses,
        rows=rows,
        periodo_label=periodo_label,
        hoje=hoje.strftime("%d/%m/%Y"),
    )
    return html


@app.route("/report")
@require_login
def report():
    html = _render_report_html(print_mode=False)
    # devolvemos html normal para o browser
    return make_response(html, 200)


@app.route("/report/pdf")
@require_login
def report_pdf():
    # para o PDF usamos a versão print_mode=True (sem botão, etc)
    html = _render_report_html(print_mode=True)

    # opções wkhtmltopdf
    options = {
        "page-size": "A4",
        "margin-top": "8mm",
        "margin-right": "8mm",
        "margin-bottom": "10mm",
        "margin-left": "8mm",
        "encoding": "UTF-8",
        "enable-local-file-access": None,
    }

    try:
        import pdfkit

        # *********** MUITO IMPORTANTE NO WINDOWS ***********
        # Muda este caminho se no teu PC estiver diferente.
        WKHTML_PATH = r"C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe"

        config = pdfkit.configuration(wkhtmltopdf=WKHTML_PATH)

        pdf_bytes = pdfkit.from_string(html, False, options=options, configuration=config)

        filename = f"relatorio_{date.today().isoformat()}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            },
        )

    except Exception as e:
        # loga no terminal para debug
        logging.exception("Erro ao gerar PDF")
        flash(f"Falha ao gerar PDF ({e}). Verifica se wkhtmltopdf está instalado e caminho correto.", "danger")
        return redirect(url_for("report"))

# ---------------------- Gestão de Utilizadores ----------------------
@app.route("/admin/users", methods=["GET", "POST"])
@require_login
def admin_users():
    user_id = session["user_id"]

    conn = get_conn()
    cur = conn.cursor()

    if request.method == "POST":
        nome = request.form.get("nome")
        email = request.form.get("email")
        senha = request.form.get("senha")
        if nome and email and senha:
            from werkzeug.security import generate_password_hash
            cur.execute(
                "INSERT INTO users (nome, email, senha) VALUES (?, ?, ?)",
                (nome, email, generate_password_hash(senha)),
            )
            conn.commit()
            flash("Novo utilizador criado com sucesso ✅", "success")

    cur.execute("SELECT id, nome, email FROM users ORDER BY id DESC")
    users = cur.fetchall()
    conn.close()

    return render_template("admin_users.html", users=users)


# ---------------------- Bootstrap ----------------------
@app.context_processor
def inject_utils():
    # Disponibiliza 'now()' e a função 'user_accounts()' dentro dos templates
    return {
        "now": datetime.now,
        "user_accounts": user_accounts,
    }

if __name__ == "__main__":
    # Inicializa a BD sem usar before_first_request (removido no Flask 3.x)
    with app.app_context():
        init_db()
    app.run(debug=True)








