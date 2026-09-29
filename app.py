from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file
import sqlite3
from pathlib import Path
from werkzeug.utils import secure_filename
import random
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()


RESEND_FROM = "onboarding@resend.dev"

def send_email(to_address, subject, body, bcc=None):
    import resend

    resend.api_key = os.getenv("RESEND_API_KEY")
    if not resend.api_key:
        raise RuntimeError("ResendのAPIキーが見つかりません。")

    recipients = [to_address]
    if bcc:
        recipients.extend(bcc)

    params = {
        "from": RESEND_FROM,
        "to": recipients,
        "subject": subject,
        "text": body,
    }

    return resend.Emails.send(params)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "events.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = "change-this-secret-key"


@app.before_request
def protect_admin():
    if request.path.startswith("/admin"):
        if request.path == "/admin/login":
            return None

        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            price TEXT NOT NULL,
            capacity INTEGER NOT NULL,
            venue TEXT NOT NULL,
            contact TEXT NOT NULL,
            organizer TEXT NOT NULL,
            recruitment_type TEXT NOT NULL,
            image_filename TEXT,
            stock_circle_threshold INTEGER NOT NULL DEFAULT 10,
            stock_circle_enabled INTEGER NOT NULL DEFAULT 1,
            is_published INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Existing databases from the previous version are upgraded safely.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "stock_circle_threshold" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN stock_circle_threshold INTEGER NOT NULL DEFAULT 10")
    if "stock_circle_enabled" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN stock_circle_enabled INTEGER NOT NULL DEFAULT 1")
    if "venue_address" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN venue_address TEXT")
    if "admin_email_recipients" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN admin_email_recipients TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            message TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(event_id) REFERENCES events(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gm_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            preferred_date TEXT,
            preferred_place TEXT,
            participants TEXT,
            game_content TEXT,
            experience TEXT,
            message TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    gm_columns = {row[1] for row in conn.execute("PRAGMA table_info(gm_requests)").fetchall()}
    if "inquiry_type" not in gm_columns:
        conn.execute("ALTER TABLE gm_requests ADD COLUMN inquiry_type TEXT NOT NULL DEFAULT 'GM依頼'")
    if "preferred_place_type" not in gm_columns:
        conn.execute("ALTER TABLE gm_requests ADD COLUMN preferred_place_type TEXT NOT NULL DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL DEFAULT "",
            image_filename TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO profile (id, content) VALUES (1, ?)",
        ("",)
    )

    # Render / 新規環境用の追加DB設定
    app_columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
    if "result_email_sent" not in app_columns:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN result_email_sent INTEGER NOT NULL DEFAULT 0"
        )

    if "status" not in gm_columns:
        conn.execute(
            "ALTER TABLE gm_requests ADD COLUMN status TEXT NOT NULL DEFAULT '未対応'"
        )

    profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(profile)").fetchall()}
    if "gm_fee" not in profile_columns:
        conn.execute("ALTER TABLE profile ADD COLUMN gm_fee TEXT NOT NULL DEFAULT ''")
    if "bio" not in profile_columns:
        conn.execute("ALTER TABLE profile ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
    if "gm_fee_detail" not in profile_columns:
        conn.execute("ALTER TABLE profile ADD COLUMN gm_fee_detail TEXT NOT NULL DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            price TEXT NOT NULL DEFAULT '',
            image_filename TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sales_url TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            image_filename TEXT NOT NULL,
            image_order INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1
        )
    """)

    conn.execute("""
        UPDATE profile
        SET
            gm_fee = CASE WHEN gm_fee = '' THEN ? ELSE gm_fee END,
            bio = CASE WHEN bio = '' THEN ? ELSE bio END,
            gm_fee_detail = CASE WHEN gm_fee_detail = '' THEN ? ELSE gm_fee_detail END
        WHERE id = 1
    """, (
        "基本料金(4時間)：500円/1人",
        "2022年4月に人狼ゲームを始めると共に、音大生人狼サークル「MusicWolf」を設立し、代表を務める。",
        "延長料金(1時間)：100円/1人"
    ))

    # 初期メールテンプレート
    email_templates = [
        (
            "reservation_accepted", "予約受付",
            "【{{event_name}}】予約受付のお知らせ",
            """{{name}} 様

「{{event_name}}」へのご予約を受け付けました。

開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
料金：{{price}}円

当日はお気をつけてお越しください。

{{organizer}}
{{contact}}"""
        ),
        (
            "waitlist", "キャンセル待ち受付",
            "【{{event_name}}】キャンセル待ち受付のお知らせ",
            """{{name}} 様

「{{event_name}}」へのお申し込みを、キャンセル待ちとして受け付けました。

キャンセルが発生した場合は、改めてご連絡いたします。

開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
料金：{{price}} 円

{{organizer}}
{{contact}}"""
        ),
        (
            "waitlist_contact", "打診",
            "【{{event_name}}】参加についてのご案内",
            """{{name}} 様

「{{event_name}}」について、参加のご案内が可能となりました。

参加をご希望の場合は、こちらのメールへご返信ください。

開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
料金：{{price}}円

{{organizer}}
{{contact}}"""
        ),
        (
            "lottery_applied", "抽選応募受付",
            "【{{event_name}}】抽選応募受付のお知らせ",
            """{{name}} 様

「{{event_name}}」への抽選応募を受け付けました。

抽選結果は改めてご案内いたします。

開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
料金：{{price}} 円

{{organizer}}
{{contact}}"""
        ),
        (
            "lottery_won", "当選",
            "【{{event_name}}】抽選結果のお知らせ",
            """{{name}} 様

「{{event_name}}」の抽選結果についてご案内いたします。

このたび、当選となりました。

開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
料金：{{price}}円

{{organizer}}
{{contact}}"""
        ),
        (
            "lottery_lost", "落選",
            "【{{event_name}}】抽選結果のお知らせ",
            """{{name}} 様

「{{event_name}}」の抽選結果についてご案内いたします。

今回は落選となりました。

ご応募いただき、ありがとうございました。

またの応募を心よりおまちしております。

{{organizer}}
{{contact}}"""
        ),
        (
            "inquiry_complete", "お問合せ完了",
            "【{{event_name}}】お問い合わせを受け付けました",
            """{{name}} 様

お問い合わせありがとうございます。

以下の内容でお問い合わせを受け付けました。

{{message}}

担当者より改めてご連絡いたします。

Ryu♪

Mail：ryu.jinro0624@gmail.com
X：https://x.com/RyuJinro0624"""
        ),
        (
            "lottery_daily_summary", "抽選応募集計",
            "【{{event_name}}】抽選応募状況のお知らせ",
            """抽選応募状況をお知らせします。

イベント名：{{event_name}}
開催日：{{event_date}}

本日の応募者数：{{today_count}}名
現在の応募者総数：{{total_count}}名
定員：{{capacity}}名

{{applicant_list}}

{{organizer}}
{{contact}}"""
        ),
        (
            "first_reservation_admin", "先着予約通知（運営）",
            "【{{event_name}}】先着予約がありました",
            """先着予約を受け付けました。

イベント名：{{event_name}}
開催日：{{event_date}}
時間：{{event_time}}
会場：{{venue}}
会場住所：{{venue_address}}
料金：{{price}}円

予約者名：{{name}}
メールアドレス：{{email}}
電話番号：{{phone}}

ご質問・ご連絡事項：
{{message}}

{{organizer}}
{{contact}}"""
        ),
        (
            "gm_request_admin", "GM依頼・お問い合わせ通知（運営）",
            "【GM依頼・お問い合わせ】新しいお問い合わせがありました",
            """新しいGM依頼・お問い合わせを受け付けました。

内容：{{inquiry_type}}

お名前：{{name}}
メールアドレス：{{email}}
電話番号：{{phone}}

希望場所：{{preferred_place_type}}
希望場所詳細：{{preferred_place}}
希望日時：{{preferred_date}}

希望ゲーム内容：
{{game_content}}

お問い合わせ・ご要望：
{{message}}

受付日時：{{created_at}}"""
        ),
    ]

    for template_key, name, subject, body in email_templates:
        conn.execute("""
            INSERT OR IGNORE INTO email_templates
            (template_key, name, subject, body, enabled)
            VALUES (?, ?, ?, ?, 1)
        """, (template_key, name, subject, body))

    # プロフィール初期データ
    conn.execute("""
        UPDATE profile
        SET
            gm_fee = CASE WHEN gm_fee = '' THEN ? ELSE gm_fee END,
            bio = CASE WHEN bio = '' THEN ? ELSE bio END,
            gm_fee_detail = CASE WHEN gm_fee_detail = '' THEN ? ELSE gm_fee_detail END
        WHERE id = 1
    """, (
        "基本料金(4時間)：500円/1人",
        "2022年4月に人狼ゲームを始めると共に、音大生人狼サークル「MusicWolf」を設立し、代表を務める。",
        "延長料金(1時間)：100円/1人"
    ))

    # 商品初期データ
    conn.execute("""
        INSERT OR IGNORE INTO products
        (id, name, description, price, image_filename, sales_url)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        1,
        "MusicWolf人狼カード",
        """人狼サークルが作った、初めての方にも優しい人狼カード

【MusicWolf 人狼カードの特徴】
・役職説明が丁寧で初めて引く役職でも安心
・オープンルール対応役職多数でより盛り上がれる
・一部役職に(A,B)とつけたことで様々な配役にも対応

【役職一覧】(全54枚/26種)
村人9/占い師2(A,B)/霊媒師2(A,B)/騎士2(A,B)/パン屋1/
ハンター9/ストーカー1/悪魔1/魔女1/共有者2/猫又1/
人狼4/異狼1/狼の子1/狂人2(A,B)/狂信者1/狂人ハンター1/
自爆狂人1/星の使徒4/星のハンター1/妖狐1/背徳者1/
ねずみ1/キューピッド1/悪女1/恋人2

制作：MusicWolf
監修：Ryu♪
イラスト：さく太""",
        "3000円（税込・送料別）",
        None,
        "https://ryujinro.booth.pm/items/7757451"
    ))

    # 商品画像初期データ
    product_images = [
        ("product_1_1_IMG_1717.jpeg", 1),
        ("product_1_2_1.png", 2),
        ("product_1_3_2.png", 3),
        ("product_1_4_3.png", 4),
        ("product_1_5_4.png", 5),
    ]

    for filename, image_order in product_images:
        conn.execute("""
            INSERT OR IGNORE INTO product_images
            (product_id, image_filename, image_order)
            VALUES (1, ?, ?)
        """, (filename, image_order))

    conn.commit()
    conn.close()

def remaining_seats(event_id):
    conn = get_db()
    event = conn.execute(
        "SELECT capacity, recruitment_type FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    if not event or event["recruitment_type"] != "first":
        conn.close()
        return None

    accepted = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE event_id = ? AND status = 'accepted'",
        (event_id,)
    ).fetchone()[0]

    contacting = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE event_id = ? AND status = 'contacting'",
        (event_id,)
    ).fetchone()[0]

    waitlist = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE event_id = ? AND status = 'waitlist'",
        (event_id,)
    ).fetchone()[0]

    conn.close()

    # キャンセル待ちがいる間は、空席を一般予約に開放しない
    if waitlist > 0:
        return 0

    # 参加確定＋打診中で確保されている席を除く
    return max(0, event["capacity"] - accepted - contacting)


def stock_display(event, remaining):
    if event["recruitment_type"] != "first":
        return None, None

    if remaining <= 0:
        return "満員", "full"

    if remaining <= 4:
        return f"残{remaining}", "orange"

    if remaining <= 9:
        return f"残{remaining}", "yellow"

    if event["stock_circle_enabled"] and remaining >= event["stock_circle_threshold"]:
        return "○", "blue"

    return f"残{remaining}", "blue"


@app.context_processor
def inject_helpers():
    return {"remaining_seats": remaining_seats, "stock_display": stock_display}


@app.route("/")
def index():
    conn = get_db()
    events = conn.execute("SELECT * FROM events WHERE is_published = 1 ORDER BY event_date, event_time").fetchall()
    conn.close()

    event_stock = {}
    for event in events:
        if event["recruitment_type"] == "first":
            remaining = remaining_seats(event["id"])
            stock_text, stock_class = stock_display(event, remaining)
            event_stock[event["id"]] = {
                "text": stock_text,
                "class": stock_class
            }

    return render_template("index.html", events=events, event_stock=event_stock)


@app.route("/event/<int:event_id>")
def event_detail(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if event is None or event["is_published"] == 0:
        return "イベントが見つかりません", 404
    remaining = remaining_seats(event_id)
    stock_text, stock_class = stock_display(event, remaining) if remaining is not None else (None, None)
    return render_template("event_detail.html", event=event, remaining=remaining,
                           stock_text=stock_text, stock_class=stock_class)


@app.route("/event/<int:event_id>/apply", methods=["GET", "POST"])
def apply_event(event_id):
    conn = get_db()

    event = conn.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    conn.close()

    if event is None or event["is_published"] == 0:
        return "イベントが見つかりません", 404

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        message = request.form.get("message", "").strip()

        if not all([name, email, phone]):
            flash("お名前・メールアドレス・電話番号は必須です。")
            remaining = remaining_seats(event_id)

            return render_template(
                "application_form.html",
                event=event,
                remaining=remaining
            )

        conn = get_db()

        existing = conn.execute(
            "SELECT id FROM applications WHERE event_id = ? AND email = ? AND status != 'cancelled'",
            (event_id, email.lower())
        ).fetchone()

        if existing:
            conn.close()
            flash("このイベントにはすでに応募済みです。")
            remaining = remaining_seats(event_id)
            return render_template(
                "application_form.html",
                event=event,
                remaining=remaining
            )

        email = email.lower()

        if event["recruitment_type"] == "first":
            accepted = conn.execute(
                "SELECT COUNT(*) FROM applications "
                "WHERE event_id = ? AND status = 'accepted'",
                (event_id,)
            ).fetchone()[0]

            contacting = conn.execute(
                "SELECT COUNT(*) FROM applications "
                "WHERE event_id = ? AND status = 'contacting'",
                (event_id,)
            ).fetchone()[0]

            waitlist = conn.execute(
                "SELECT COUNT(*) FROM applications "
                "WHERE event_id = ? AND status = 'waitlist'",
                (event_id,)
            ).fetchone()[0]

            if waitlist > 0 or accepted + contacting >= event["capacity"]:
                status = "waitlist"
            else:
                status = "accepted"

        else:
            status = "pending"

        conn.execute("""
            INSERT INTO applications
            (event_id, name, email, phone, message, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            event_id,
            name,
            email,
            phone,
            message,
            status
        ))

        conn.commit()
        conn.close()

        # 抽選型の応募受付メールを送信
        if event["recruitment_type"] == "lottery" and status == "pending":

            conn = get_db()

            template = conn.execute(
                "SELECT * FROM email_templates WHERE template_key = 'lottery_applied'"
            ).fetchone()

            conn.close()

            if template and template["enabled"]:

                price = (
                    "無料"
                    if int(event["price"]) == 0
                    else f'{event["price"]}円'
                )

                values = {
                    "{{name}}": name,
                    "{{email}}": email,
                    "{{event_name}}": event["name"],
                    "{{event_date}}": event["event_date"],
                    "{{event_time}}": event["event_time"],
                    "{{venue}}": event["venue"],
                    "{{price}}": price,
                    "{{organizer}}": event["organizer"],
                    "{{contact}}": event["contact"],
                    "{{message}}": message,
                }

                subject = template["subject"]
                body = template["body"]

                for key, value in values.items():
                    subject = subject.replace(key, str(value))
                    body = body.replace(key, str(value))

                try:
                    send_email(
                        email,
                        subject,
                        body
                    )
                except Exception as e:
                    flash(
                        f"抽選応募は受け付けましたが、受付メールの送信に失敗しました。{e}"
                    )

        # 先着型で参加確定になった場合、予約受付メールを送信
        if event["recruitment_type"] == "first" and status == "accepted":
            # 運営への先着予約通知
            conn = get_db()
            admin_template = conn.execute(
                "SELECT * FROM email_templates WHERE template_key = 'first_reservation_admin'"
            ).fetchone()
            conn.close()

            if admin_template and admin_template["enabled"]:
                price = event["price"] if event["price"] else "無料"
                admin_values = {
                    "{{name}}": name,
                    "{{email}}": email,
                    "{{phone}}": phone,
                    "{{event_name}}": event["name"],
                    "{{event_date}}": event["event_date"],
                    "{{event_time}}": event["event_time"],
                    "{{venue}}": event["venue"],
                    "{{venue_address}}": event["venue_address"] or "",
                    "{{price}}": price,
                    "{{organizer}}": event["organizer"],
                    "{{contact}}": event["contact"],
                    "{{message}}": message,
                }

                admin_subject = admin_template["subject"]
                admin_body = admin_template["body"]

                for key, value in admin_values.items():
                    admin_subject = admin_subject.replace(key, str(value))
                    admin_body = admin_body.replace(key, str(value))

                admin_recipients = [os.getenv("GMAIL_ADDRESS")]
                extra_recipients = (event["admin_email_recipients"] or "").replace("、", ",")
                admin_recipients.extend(
                    address.strip()
                    for address in extra_recipients.split(",")
                    if address.strip()
                )
                admin_recipients = list(dict.fromkeys(
                    address for address in admin_recipients if address
                ))

                for recipient in admin_recipients:
                    try:
                        send_email(recipient, admin_subject, admin_body)
                    except Exception as e:
                        app.logger.exception(
                            "管理者への予約通知メール送信に失敗しました: %s", e
                        )


            conn = get_db()

            template = conn.execute(
                "SELECT * FROM email_templates WHERE template_key = 'reservation_accepted'"
            ).fetchone()

            conn.close()

            if template and template["enabled"]:

                price = (
                    "無料"
                    if int(event["price"]) == 0
                    else f'{event["price"]}円'
                )

                values = {
                    "{{name}}": name,
                    "{{email}}": email,
                    "{{event_name}}": event["name"],
                    "{{event_date}}": event["event_date"],
                    "{{event_time}}": event["event_time"],
                    "{{venue}}": event["venue"],
                    "{{price}}": price,
                    "{{organizer}}": event["organizer"],
                    "{{contact}}": event["contact"],
                    "{{message}}": message,
                }

                subject = template["subject"]
                body = template["body"]

                for key, value in values.items():
                    subject = subject.replace(key, str(value))
                    body = body.replace(key, str(value))

                try:
                    send_email(
                        email,
                        subject,
                        body
                    )
                except Exception as e:
                    flash(
                        f"予約は受け付けましたが、確認メールの送信に失敗しました。{e}"
                    )

            flash("予約を受け付けました。")

        elif event["recruitment_type"] == "first":

            # キャンセル待ち受付メールを送信
            conn = get_db()

            template = conn.execute(
                "SELECT * FROM email_templates WHERE template_key = 'waitlist'"
            ).fetchone()

            conn.close()

            if template and template["enabled"]:

                price = (
                    "無料"
                    if int(event["price"]) == 0
                    else f'{event["price"]}円'
                )

                values = {
                    "{{name}}": name,
                    "{{email}}": email,
                    "{{event_name}}": event["name"],
                    "{{event_date}}": event["event_date"],
                    "{{event_time}}": event["event_time"],
                    "{{venue}}": event["venue"],
                    "{{price}}": price,
                    "{{organizer}}": event["organizer"],
                    "{{contact}}": event["contact"],
                    "{{message}}": message,
                }

                subject = template["subject"]
                body = template["body"]

                for key, value in values.items():
                    subject = subject.replace(key, str(value))
                    body = body.replace(key, str(value))

                try:
                    send_email(
                        email,
                        subject,
                        body
                    )
                except Exception as e:
                    flash(
                        f"キャンセル待ちは受け付けましたが、確認メールの送信に失敗しました。{e}"
                    )

            flash(
                "定員に達しているため、キャンセル待ちとして受け付けました。"
            )

        else:

            flash("抽選への応募を受け付けました。")

        return render_template(
            "application_complete.html",
            event=event,
            status=status
        )

    remaining = remaining_seats(event_id)

    return render_template(
        "application_form.html",
        event=event,
        remaining=remaining
    )


@app.route("/gm", methods=["GET", "POST"])
def gm_request():

    if request.method == "POST":

        inquiry_type = request.form.get("inquiry_type", "GM依頼").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()

        preferred_place_type = request.form.get("preferred_place_type", "").strip()
        preferred_place = request.form.get("preferred_place", "").strip()
        preferred_date = request.form.get("preferred_date", "").strip()
        game_content = request.form.get("game_content", "").strip()
        message = request.form.get("message", "").strip()

        if inquiry_type == "その他お問い合わせ":
            message = request.form.get("inquiry_message", "").strip()
            preferred_place_type = ""
            preferred_place = ""
            preferred_date = ""
            game_content = ""

        if not name or not email or not phone:
            flash("お名前・メールアドレス・電話番号は必須です。")
            return render_template("gm_form.html")

        values = (
            name,
            email,
            phone,
            preferred_date,
            preferred_place,
            "",
            game_content,
            "",
            message,
            inquiry_type,
            preferred_place_type
        )

        conn = get_db()
        conn.execute("""
            INSERT INTO gm_requests
            (name, email, phone, preferred_date, preferred_place, participants,
             game_content, experience, message, inquiry_type, preferred_place_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)

        conn.commit()
        conn.close()

        # 運営へのGM依頼・お問い合わせ通知メール
        conn = get_db()
        admin_template = conn.execute(
            "SELECT * FROM email_templates WHERE template_key = 'gm_request_admin'"
        ).fetchone()
        conn.close()

        if admin_template and admin_template["enabled"]:
            admin_values = {
                "{{inquiry_type}}": inquiry_type,
                "{{name}}": name,
                "{{email}}": email,
                "{{phone}}": phone,
                "{{preferred_place_type}}": preferred_place_type,
                "{{preferred_place}}": preferred_place,
                "{{preferred_date}}": preferred_date,
                "{{game_content}}": game_content,
                "{{message}}": message,
                "{{created_at}}": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            admin_subject = admin_template["subject"]
            admin_body = admin_template["body"]

            for key, value in admin_values.items():
                admin_subject = admin_subject.replace(key, str(value))
                admin_body = admin_body.replace(key, str(value))

            admin_email = os.getenv("GMAIL_ADDRESS")

            if admin_email:
                try:
                    send_email(admin_email, admin_subject, admin_body)
                except Exception as e:
                    print(f"運営通知メール送信失敗: {e}")

        # お問合せ完了メール
        conn = get_db()
        inquiry_template = conn.execute(
            "SELECT * FROM email_templates WHERE template_key = 'inquiry_complete'"
        ).fetchone()
        conn.close()

        if inquiry_template and inquiry_template["enabled"]:

            if inquiry_type == "GM依頼":
                email_message = f"""内容：GM依頼
希望場所：{preferred_place_type}
希望場所詳細：{preferred_place}
希望日時：{preferred_date}
希望ゲーム内容：{game_content}
その他ご要望：
{message}"""
            else:
                email_message = message

            values = {
                "{{name}}": name,
                "{{email}}": email,
                "{{phone}}": phone,
                "{{inquiry_type}}": inquiry_type,
                "{{preferred_place_type}}": preferred_place_type,
                "{{preferred_place}}": preferred_place,
                "{{preferred_date}}": preferred_date,
                "{{game_content}}": game_content,
                "{{message}}": email_message,
            }

            subject = inquiry_template["subject"]
            body = inquiry_template["body"]

            for key, value in values.items():
                subject = subject.replace(key, str(value))
                body = body.replace(key, str(value))

            try:
                send_email(email, subject, body)
            except Exception as e:
                print(f"お問合せ完了メール送信失敗: {e}")

        if inquiry_type == "GM依頼":
            flash("GM依頼を受け付けました。")
        else:
            flash("お問い合わせを受け付けました。")

        return redirect(url_for("index"))

    return render_template("gm_form.html")


@app.route("/admin/email-templates")
def email_templates():
    conn = get_db()
    templates = conn.execute(
        "SELECT * FROM email_templates ORDER BY id"
    ).fetchall()
    conn.close()

    return render_template(
        "email_templates.html",
        templates=templates
    )


@app.route("/admin/email-template/<int:template_id>", methods=["GET", "POST"])
def edit_email_template(template_id):
    conn = get_db()

    template = conn.execute(
        "SELECT * FROM email_templates WHERE id = ?",
        (template_id,)
    ).fetchone()

    if template is None:
        conn.close()
        return "メールテンプレートが見つかりません", 404

    if request.method == "POST":
        name = template["name"]
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "")
        enabled = 1 if request.form.get("enabled") else 0

        conn.execute(
            """
            UPDATE email_templates
            SET name = ?, subject = ?, body = ?, enabled = ?
            WHERE id = ?
            """,
            (name, subject, body, enabled, template_id)
        )

        conn.commit()
        conn.close()

        flash("メールテンプレートを保存しました。")
        return redirect(url_for("email_templates"))

    conn.close()

    return render_template(
        "email_template_form.html",
        template=template
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if (
            username == os.getenv("ADMIN_USERNAME")
            and password == os.getenv("ADMIN_PASSWORD")
        ):
            session["admin_logged_in"] = True
            return redirect(url_for("admin"))

        flash("ユーザー名またはパスワードが正しくありません。")

    return render_template("admin_login.html")


@app.route("/admin")
def admin():
    conn = get_db()
    events = conn.execute("SELECT * FROM events ORDER BY event_date, event_time").fetchall()
    conn.close()
    return render_template("admin.html", events=events)



@app.route("/admin/backup")
def admin_backup():
    if not session.get("admin_logged_in"):
        return redirect(url_for("admin_login"))

    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name="events_backup.db"
    )


@app.route("/profile")
def profile():
    conn = get_db()

    profile_row = conn.execute(
        "SELECT * FROM profile WHERE id = 1"
    ).fetchone()

    products = conn.execute(
        "SELECT * FROM products ORDER BY id"
    ).fetchall()

    images = conn.execute(
        "SELECT * FROM product_images ORDER BY product_id, image_order"
    ).fetchall()

    images_by_product = {}

    for image in images:
        images_by_product.setdefault(image["product_id"], []).append(image)

    conn.close()

    return render_template(
        "profile.html",
        profile=profile_row,
        products=products,
        images_by_product=images_by_product
    )


@app.route("/admin/profile", methods=["GET", "POST"])
def profile_admin():
    conn = get_db()

    if request.method == "POST":
        gm_fee = request.form.get("gm_fee", "").strip()
        gm_fee_detail = request.form.get("gm_fee_detail", "").strip()
        bio = request.form.get("bio", "").strip()

        conn.execute(
            """
            UPDATE profile
            SET gm_fee = ?, gm_fee_detail = ?, bio = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (gm_fee, gm_fee_detail, bio)
        )
        conn.commit()
        conn.close()

        flash("Ryu♪とはの内容を保存しました。")
        return redirect(url_for("profile_admin"))

    profile_row = conn.execute(
        "SELECT * FROM profile WHERE id = 1"
    ).fetchone()

    products = conn.execute(
        "SELECT * FROM products ORDER BY id"
    ).fetchall()

    conn.close()

    return render_template(
        "profile_admin.html",
        profile=profile_row,
        products=products
    )


@app.route("/admin/profile/product/add", methods=["GET", "POST"])
def product_add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price = request.form.get("price", "").strip()
        sales_url = request.form.get("sales_url", "").strip()

        if not name:
            flash("商品名を入力してください。")
            return render_template("product_form.html", product=None)

        conn = get_db()

        cursor = conn.execute(
            """
            INSERT INTO products (name, description, price, sales_url)
            VALUES (?, ?, ?, ?)
            """,
            (name, description, price, sales_url)
        )

        product_id = cursor.lastrowid

        upload_dir = Path(app.static_folder) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        for i in range(1, 6):
            file = request.files.get(f"image_{i}")

            if file and file.filename:
                filename = f"product_{product_id}_{i}_{file.filename}"
                file.save(upload_dir / filename)

                conn.execute(
                    """
                    INSERT INTO product_images
                    (product_id, image_filename, image_order)
                    VALUES (?, ?, ?)
                    """,
                    (product_id, filename, i)
                )

        conn.commit()
        conn.close()

        flash("商品を追加しました。")
        return redirect(url_for("profile_admin"))

    return render_template("product_form.html", product=None)


@app.route("/admin/profile/product/<int:product_id>/edit", methods=["GET", "POST"])
def product_edit(product_id):
    conn = get_db()

    product = conn.execute(
        "SELECT * FROM products WHERE id = ?",
        (product_id,)
    ).fetchone()

    if not product:
        conn.close()
        flash("商品が見つかりません。")
        return redirect(url_for("profile_admin"))

    images = conn.execute(
        "SELECT * FROM product_images WHERE product_id = ? ORDER BY image_order",
        (product_id,)
    ).fetchall()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price = request.form.get("price", "").strip()
        sales_url = request.form.get("sales_url", "").strip()

        if not name:
            conn.close()
            flash("商品名を入力してください。")
            return render_template(
                "product_form.html",
                product=product,
                images=images
            )

        conn.execute(
            """
            UPDATE products
            SET name = ?, description = ?, price = ?, sales_url = ?
            WHERE id = ?
            """,
            (name, description, price, sales_url, product_id)
        )

        upload_dir = Path(app.static_folder) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        for i in range(1, 6):
            file = request.files.get(f"image_{i}")

            if file and file.filename:
                old_image = conn.execute(
                    """
                    SELECT * FROM product_images
                    WHERE product_id = ? AND image_order = ?
                    """,
                    (product_id, i)
                ).fetchone()

                filename = f"product_{product_id}_{i}_{file.filename}"
                file.save(upload_dir / filename)

                if old_image:
                    old_path = upload_dir / old_image["image_filename"]
                    if old_path.exists():
                        old_path.unlink()

                    conn.execute(
                        """
                        UPDATE product_images
                        SET image_filename = ?
                        WHERE id = ?
                        """,
                        (filename, old_image["id"])
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO product_images
                        (product_id, image_filename, image_order)
                        VALUES (?, ?, ?)
                        """,
                        (product_id, filename, i)
                    )

        conn.commit()
        conn.close()

        flash("商品を更新しました。")
        return redirect(url_for("profile_admin"))

    conn.close()

    return render_template(
        "product_form.html",
        product=product,
        images=images
    )


@app.route("/admin/profile/product/<int:product_id>/delete", methods=["POST"])
def product_delete(product_id):
    conn = get_db()

    product = conn.execute(
        "SELECT * FROM products WHERE id = ?",
        (product_id,)
    ).fetchone()

    if not product:
        conn.close()
        flash("商品が見つかりません。")
        return redirect(url_for("profile_admin"))

    images = conn.execute(
        "SELECT * FROM product_images WHERE product_id = ?",
        (product_id,)
    ).fetchall()

    upload_dir = Path(app.static_folder) / "uploads"

    for image in images:
        image_path = upload_dir / image["image_filename"]
        if image_path.exists():
            image_path.unlink()

    conn.execute(
        "DELETE FROM product_images WHERE product_id = ?",
        (product_id,)
    )

    conn.execute(
        "DELETE FROM products WHERE id = ?",
        (product_id,)
    )

    conn.commit()
    conn.close()

    flash("商品を削除しました。")
    return redirect(url_for("profile_admin"))


@app.route("/admin/gm-requests")
def gm_requests():
    conn = get_db()
    requests = conn.execute(
        "SELECT * FROM gm_requests ORDER BY created_at DESC, id DESC"
    ).fetchall()
    conn.close()

    return render_template(
        "gm_requests.html",
        requests=requests
    )


@app.route("/admin/gm-request/<int:request_id>", methods=["GET", "POST"])
def gm_request_detail(request_id):
    conn = get_db()
    gm_request_row = conn.execute(
        "SELECT * FROM gm_requests WHERE id = ?",
        (request_id,)
    ).fetchone()
    conn.close()

    if not gm_request_row:
        flash("お問い合わせが見つかりません。")
        return redirect(url_for("gm_requests"))

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        # 対応状況の変更
        if action == "update_status":
            status = request.form.get("status", "未対応").strip()

            if status not in ["未対応", "対応中", "対応済み"]:
                flash("対応状況が正しくありません。")
                return redirect(
                    url_for("gm_request_detail", request_id=request_id)
                )

            conn = get_db()
            conn.execute(
                "UPDATE gm_requests SET status = ? WHERE id = ?",
                (status, request_id)
            )
            conn.commit()
            conn.close()

            flash("対応状況を更新しました。")
            return redirect(
                url_for("gm_request_detail", request_id=request_id)
            )

        # 依頼者へのメール送信
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()

        if not subject or not body:
            flash("件名と本文を入力してください。")
            return render_template(
                "gm_request_detail.html",
                gm_request=gm_request_row
            )

        try:
            send_email(
                gm_request_row["email"],
                subject,
                body
            )
            flash("メールを送信しました。")
        except Exception as e:
            print(f"お問い合わせ返信メール送信失敗: {e}")
            flash("メールの送信に失敗しました。")

        return redirect(
            url_for("gm_request_detail", request_id=request_id)
        )

    return render_template(
        "gm_request_detail.html",
        gm_request=gm_request_row
    )


@app.route("/admin/event/new", methods=["GET", "POST"])
def new_event():
    if request.method == "POST":
        image_filename = None
        image = request.files.get("image")
        if image and image.filename:
            image_filename = secure_filename(image.filename)
            image.save(UPLOAD_DIR / image_filename)
        threshold = max(1, int(request.form.get("stock_circle_threshold", 10)))
        enabled = 1 if request.form.get("stock_circle_enabled") == "on" else 0
        data = (
            request.form["name"], request.form["description"], request.form["event_date"],
            request.form["event_time"], request.form["price"], int(request.form["capacity"]),
            request.form["venue"], request.form.get("venue_address", "").strip(),
            request.form["contact"], request.form.get("admin_email_recipients", "").strip(),
            request.form["organizer"], request.form["recruitment_type"], image_filename,
            threshold, enabled,
            0 if request.form.get("save_type") == "draft"
            else int(request.form.get("publication_status", 1))
        )
        conn = get_db()
        conn.execute("""
            INSERT INTO events
            (name, description, event_date, event_time, price, capacity, venue, venue_address,
             contact, admin_email_recipients, organizer, recruitment_type, image_filename,
             stock_circle_threshold, stock_circle_enabled, is_published)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)
        conn.commit()
        conn.close()
        flash("イベントを登録しました。")
        return redirect(url_for("admin"))
    return render_template("event_form.html", event=None)


@app.route("/admin/event/<int:event_id>/edit", methods=["GET", "POST"])
def edit_event(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if event is None:
        return "イベントが見つかりません", 404
    if request.method == "POST":
        image_filename = event["image_filename"]
        image = request.files.get("image")
        if image and image.filename:
            image_filename = secure_filename(image.filename)
            image.save(UPLOAD_DIR / image_filename)
        threshold = max(1, int(request.form.get("stock_circle_threshold", 10)))
        enabled = 1 if request.form.get("stock_circle_enabled") == "on" else 0
        conn = get_db()
        is_published = (
            0 if request.form.get("save_type") == "draft"
            else int(request.form.get("publication_status", event["is_published"]))
        )

        conn.execute("""
            UPDATE events SET name=?, description=?, event_date=?, event_time=?, price=?, capacity=?,
            venue=?, venue_address=?, contact=?, admin_email_recipients=?,
            organizer=?, recruitment_type=?, image_filename=?,
            stock_circle_threshold=?, stock_circle_enabled=?, is_published=? WHERE id=?
        """, (
            request.form["name"], request.form["description"], request.form["event_date"],
            request.form["event_time"], request.form["price"], int(request.form["capacity"]),
            request.form["venue"], request.form.get("venue_address", "").strip(),
            request.form["contact"], request.form.get("admin_email_recipients", "").strip(),
            request.form["organizer"], request.form["recruitment_type"], image_filename,
            threshold, enabled, is_published, event_id
        ))
        conn.commit()
        conn.close()
        flash("イベントを更新しました。")
        return redirect(url_for("admin"))
    return render_template("event_form.html", event=event)


@app.route("/admin/event/<int:event_id>/publication", methods=["POST"])
def set_event_publication(event_id):
    status = request.form.get("publication_status", "0")

    if status not in {"0", "1", "2"}:
        return "公開状況が不正です", 400

    conn = get_db()
    event = conn.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    if event is None:
        conn.close()
        return "イベントが見つかりません", 404

    conn.execute(
        "UPDATE events SET is_published = ? WHERE id = ?",
        (int(status), event_id)
    )
    conn.commit()
    conn.close()

    labels = {
        "0": "非公開",
        "1": "公開",
        "2": "限定公開"
    }
    flash(f"公開状況を「{labels[status]}」に変更しました。")
    return redirect(url_for("admin"))


@app.route("/admin/event/<int:event_id>/toggle", methods=["POST"])
def toggle_event_visibility(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if event is None:
        conn.close()
        return "イベントが見つかりません", 404

    new_status = 0 if event["is_published"] else 1
    conn.execute(
        "UPDATE events SET is_published = ? WHERE id = ?",
        (new_status, event_id)
    )
    conn.commit()
    conn.close()

    flash("イベントを公開しました。" if new_status else "イベントを非公開にしました。")
    return redirect(url_for("admin"))


@app.route("/admin/event/<int:event_id>/delete", methods=["POST"])
def delete_event(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if event:
        conn.execute("DELETE FROM applications WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        if event["image_filename"]:
            image_path = UPLOAD_DIR / event["image_filename"]
            if image_path.exists():
                image_path.unlink()
    conn.close()
    flash("イベントを削除しました。")
    return redirect(url_for("admin"))


def send_daily_lottery_summary():
    """日本時間の当日分の抽選応募を管理者へ集計送信する。"""
    JST = timezone(timedelta(hours=9))
    now_jst = datetime.now(JST)
    today_jst = now_jst.date()

    start_jst = datetime(
        today_jst.year, today_jst.month, today_jst.day,
        tzinfo=JST
    )
    end_jst = start_jst + timedelta(days=1)

    start_utc = start_jst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_jst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()

    events = conn.execute(
        "SELECT * FROM events WHERE recruitment_type = 'lottery' "
        "AND is_published = 1 ORDER BY event_date, id"
    ).fetchall()

    admin_email = os.getenv("GMAIL_ADDRESS")

    if not admin_email:
        conn.close()
        raise RuntimeError("GMAIL_ADDRESSが設定されていません。")

    sent_count = 0

    for event in events:
        applications = conn.execute(
            "SELECT * FROM applications "
            "WHERE event_id = ? "
            "AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at, id",
            (event["id"], start_utc, end_utc)
        ).fetchall()

        if not applications:
            continue

        template = conn.execute(
            "SELECT * FROM email_templates "
            "WHERE template_key = 'lottery_daily_summary'"
        ).fetchone()

        if not template or not template["enabled"]:
            continue

        applicant_lines = []
        for i, app_row in enumerate(applications, 1):
            applicant_lines.append(
                f"{i}. {app_row['name']}（{app_row['email']}）"
            )

        values = {
            "{{event_name}}": event["name"],
            "{{event_date}}": event["event_date"],
            "{{capacity}}": event["capacity"],
            "{{today_count}}": len(applications),
            "{{total_count}}": conn.execute(
                "SELECT COUNT(*) FROM applications WHERE event_id = ?",
                (event["id"],)
            ).fetchone()[0],
            "{{applicant_list}}": "\n".join(applicant_lines),
            "{{organizer}}": event["organizer"],
            "{{contact}}": event["contact"],
        }

        subject = template["subject"]
        body = template["body"]

        for key, value in values.items():
            subject = subject.replace(key, str(value))
            body = body.replace(key, str(value))

        send_email(admin_email, subject, body)
        sent_count += 1

    conn.close()
    return sent_count


@app.route("/admin/test-lottery-summary")
def test_lottery_summary():
    count = send_daily_lottery_summary()
    return f"抽選応募集計メールを{count}件送信しました。"


@app.route("/admin/event/<int:event_id>/applications")
def event_applications(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    applications = conn.execute(
        "SELECT * FROM applications WHERE event_id = ? ORDER BY created_at DESC, id DESC", (event_id,)
    ).fetchall()
    conn.close()
    if event is None:
        return "イベントが見つかりません", 404
    return render_template("applications.html", event=event, applications=applications)


@app.route("/admin/event/<int:event_id>/bulk-email", methods=["GET", "POST"])
def event_bulk_email(event_id):

    conn = get_db()

    event = conn.execute(
        "SELECT * FROM events WHERE id = ?",
        (event_id,)
    ).fetchone()

    if not event:
        conn.close()
        return "イベントが見つかりません", 404

    participants = conn.execute(
        "SELECT * FROM applications "
        "WHERE event_id = ? AND status IN ('accepted', 'won') "
        "ORDER BY id",
        (event_id,)
    ).fetchall()

    conn.close()

    admin_email = os.getenv("GMAIL_ADDRESS", "")

    if request.method == "POST":

        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()

        if not subject or not body:
            flash("件名と本文を入力してください。")
            return render_template(
                "event_bulk_email.html",
                event=event,
                participants=participants,
                admin_email=admin_email
            )

        admin_email = os.getenv("GMAIL_ADDRESS")

        if not admin_email:
            flash("管理者メールアドレスが設定されていません。")
            return redirect(url_for("event_bulk_email", event_id=event_id))

        participant_emails = [
            participant["email"]
            for participant in participants
            if participant["email"]
        ]

        if not participant_emails:
            flash("参加予定者がいません。")
            return redirect(url_for("event_bulk_email", event_id=event_id))

        try:
            send_email(
                admin_email,
                subject,
                body,
                bcc=participant_emails
            )
            flash(f"管理者宛に送信し、参加予定者{len(participant_emails)}名をBCCに追加しました。")
        except Exception as e:
            print(f"参加者一斉メール送信失敗: {e}")
            flash("メールの送信に失敗しました。")

        return redirect(url_for("event_bulk_email", event_id=event_id))

    return render_template(
        "event_bulk_email.html",
        event=event,
        participants=participants,
        admin_email=admin_email
    )


@app.route("/admin/event/<int:event_id>/lottery/send-results", methods=["POST"])
def send_lottery_results(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if not event:
        conn.close()
        return "イベントが見つかりません", 404

    applications = conn.execute(
        "SELECT * FROM applications "
        "WHERE event_id = ? AND status IN ('won', 'lost') "
        "AND result_email_sent = 0 ORDER BY id",
        (event_id,)
    ).fetchall()

    sent_count = 0
    failed_count = 0

    templates = {}
    for key in ("lottery_won", "lottery_lost"):
        template = conn.execute(
            "SELECT * FROM email_templates WHERE template_key = ?",
            (key,)
        ).fetchone()
        if template and template["enabled"]:
            templates[key] = template

    for app_row in applications:
        template_key = "lottery_won" if app_row["status"] == "won" else "lottery_lost"
        template = templates.get(template_key)

        if not template:
            failed_count += 1
            continue

        price = "無料" if int(event["price"]) == 0 else f'{event["price"]}円'

        values = {
            "{{name}}": app_row["name"],
            "{{email}}": app_row["email"],
            "{{event_name}}": event["name"],
            "{{event_date}}": event["event_date"],
            "{{event_time}}": event["event_time"],
            "{{venue}}": event["venue"],
            "{{price}}": price,
            "{{organizer}}": event["organizer"],
            "{{contact}}": event["contact"],
            "{{message}}": app_row["message"] or "",
        }

        subject = template["subject"]
        body = template["body"]

        for key, value in values.items():
            subject = subject.replace(key, str(value))
            body = body.replace(key, str(value))

        try:
            send_email(app_row["email"], subject, body)

            conn.execute(
                "UPDATE applications SET result_email_sent = 1 WHERE id = ?",
                (app_row["id"],)
            )

            sent_count += 1

        except Exception:
            failed_count += 1

    conn.commit()
    conn.close()

    if failed_count:
        flash(f"当落メールを{sent_count}件送信しました（未送信{failed_count}件）。")
    else:
        flash(f"当落メールを{sent_count}件送信しました。")

    return redirect(url_for("event_applications", event_id=event_id))


@app.route("/admin/event/<int:event_id>/applications/update", methods=["POST"])
def update_applications(event_id):
    conn = get_db()
    applications = conn.execute(
        "SELECT id FROM applications WHERE event_id = ?", (event_id,)
    ).fetchall()

    allowed = {"accepted", "waitlist", "contacting", "cancelled", "pending", "won", "lost"}

    for item in applications:
        status = request.form.get(f"status_{item['id']}")
        if status in allowed:
            current = conn.execute(
                "SELECT * FROM applications WHERE id = ?",
                (item["id"],)
            ).fetchone()

            # 打診中 → 参加確定の場合、参加確定メールを送信
            if current["status"] == "contacting" and status == "accepted":

                event = conn.execute(
                    "SELECT * FROM events WHERE id = ?",
                    (event_id,)
                ).fetchone()

                template = conn.execute(
                    "SELECT * FROM email_templates WHERE template_key = 'reservation_accepted'"
                ).fetchone()

                if template and template["enabled"]:
                    price = "無料" if int(event["price"]) == 0 else f'{event["price"]}円'

                    values = {
                        "{{name}}": current["name"],
                        "{{email}}": current["email"],
                        "{{event_name}}": event["name"],
                        "{{event_date}}": event["event_date"],
                        "{{event_time}}": event["event_time"],
                        "{{venue}}": event["venue"],
                        "{{price}}": price,
                        "{{organizer}}": event["organizer"],
                        "{{contact}}": event["contact"],
                        "{{message}}": current["message"] or "",
                    }

                    subject = template["subject"]
                    body = template["body"]

                    for key, value in values.items():
                        subject = subject.replace(key, str(value))
                        body = body.replace(key, str(value))

                    send_email(current["email"], subject, body)

            conn.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (status, item["id"])
            )

    conn.commit()
    conn.close()

    return redirect(url_for("event_applications", event_id=event_id))


@app.route("/admin/application/<int:application_id>/promote", methods=["POST"])
def promote_waitlist(application_id):
    conn = get_db()

    app_row = conn.execute(
        "SELECT * FROM applications WHERE id = ?",
        (application_id,)
    ).fetchone()

    if not app_row:
        conn.close()
        return "申込が見つかりません", 404

    if app_row["status"] != "waitlist":
        conn.close()
        return "キャンセル待ちの申込ではありません", 400

    event = conn.execute(
        "SELECT * FROM events WHERE id = ?",
        (app_row["event_id"],)
    ).fetchone()

    template = conn.execute(
        "SELECT * FROM email_templates WHERE template_key = 'waitlist_contact'"
    ).fetchone()

    if event is None:
        conn.close()
        return "イベントが見つかりません", 404

    try:
        if template and template["enabled"]:
            price = "無料" if int(event["price"]) == 0 else f'{event["price"]}円'

            values = {
                "{{name}}": app_row["name"],
                "{{email}}": app_row["email"],
                "{{event_name}}": event["name"],
                "{{event_date}}": event["event_date"],
                "{{event_time}}": event["event_time"],
                "{{venue}}": event["venue"],
                "{{price}}": price,
                "{{organizer}}": event["organizer"],
                "{{contact}}": event["contact"],
                "{{message}}": app_row["message"] or "",
            }

            subject = template["subject"]
            body = template["body"]

            for key, value in values.items():
                subject = subject.replace(key, str(value))
                body = body.replace(key, str(value))

            send_email(
                app_row["email"],
                subject,
                body
            )

        conn.execute(
            "UPDATE applications SET status = 'contacting' WHERE id = ?",
            (application_id,)
        )

        conn.commit()

    except Exception as e:
        conn.rollback()
        conn.close()
        return f"メール送信に失敗したため、打診中には変更していません。\n{e}", 500

    conn.close()

    flash("打診メールを送信し、ステータスを「打診中」に変更しました。")

    return redirect(url_for(
        "event_applications",
        event_id=app_row["event_id"]
    ))


@app.route("/admin/application/<int:application_id>/delete", methods=["POST"])
def delete_application(application_id):
    conn = get_db()
    app_row = conn.execute(
        "SELECT * FROM applications WHERE id = ?", (application_id,)
    ).fetchone()

    if not app_row:
        conn.close()
        return "申込が見つかりません", 404

    if app_row["status"] != "cancelled":
        conn.close()
        return "キャンセル済みの申込のみ削除できます", 400

    event_id = app_row["event_id"]

    conn.execute(
        "DELETE FROM applications WHERE id = ?", (application_id,)
    )
    conn.commit()
    conn.close()

    return redirect(url_for("event_applications", event_id=event_id))


@app.route("/admin/application/<int:application_id>/status", methods=["POST"])
def application_status(application_id):
    new_status = request.form.get("status")

    allowed = {"accepted", "waitlist", "contacting", "cancelled", "pending", "won", "lost"}

    if new_status not in allowed:
        return "不正なステータスです", 400

    conn = get_db()

    app_row = conn.execute(
        "SELECT * FROM applications WHERE id = ?",
        (application_id,)
    ).fetchone()

    if not app_row:
        conn.close()
        return "申込が見つかりません", 404

    old_status = app_row["status"]

    if old_status == "contacting" and new_status == "accepted":
        event = conn.execute(
            "SELECT * FROM events WHERE id = ?",
            (app_row["event_id"],)
        ).fetchone()

        template = conn.execute(
            "SELECT * FROM email_templates WHERE template_key = 'reservation_accepted'"
        ).fetchone()

        if event is None:
            conn.close()
            return "イベントが見つかりません", 404

        try:
            if template and template["enabled"]:
                price = "無料" if int(event["price"]) == 0 else f'{event["price"]}円'

                values = {
                    "{{name}}": app_row["name"],
                    "{{email}}": app_row["email"],
                    "{{event_name}}": event["name"],
                    "{{event_date}}": event["event_date"],
                    "{{event_time}}": event["event_time"],
                    "{{venue}}": event["venue"],
                    "{{price}}": price,
                    "{{organizer}}": event["organizer"],
                    "{{contact}}": event["contact"],
                    "{{message}}": app_row["message"] or "",
                }

                subject = template["subject"]
                body = template["body"]

                for key, value in values.items():
                    subject = subject.replace(key, str(value))
                    body = body.replace(key, str(value))

                send_email(app_row["email"], subject, body)

            conn.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (new_status, application_id)
            )

            conn.commit()

        except Exception as e:
            conn.rollback()
            conn.close()
            return f"メール送信に失敗したため、参加確定には変更していません。\\n{e}", 500

        conn.close()

        flash("参加確定メールを送信し、ステータスを「参加確定」に変更しました。")

        return redirect(url_for(
            "event_applications",
            event_id=app_row["event_id"]
        ))

    conn.execute(
        "UPDATE applications SET status = ? WHERE id = ?",
        (new_status, application_id)
    )

    conn.commit()
    conn.close()

    return redirect(url_for(
        "event_applications",
        event_id=app_row["event_id"]
    ))


@app.route("/admin/lottery/<int:event_id>", methods=["POST"])
def run_lottery(event_id):
    conn = get_db()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        conn.close()
        return "イベントが見つかりません", 404
    pending = conn.execute(
        "SELECT id FROM applications WHERE event_id = ? AND status = 'pending'", (event_id,)
    ).fetchall()
    ids = [row["id"] for row in pending]
    random.shuffle(ids)
    winners = set(ids[:event["capacity"]])
    for app_id in ids:
        conn.execute("UPDATE applications SET status = ? WHERE id = ?", ("won" if app_id in winners else "lost", app_id))
    conn.commit()
    conn.close()
    flash(f"抽選を実行しました（当選{len(winners)}名）。")
    return redirect(url_for("event_applications", event_id=event_id))


init_db()

if __name__ == "__main__":
    app.run(debug=False, port=5001)
