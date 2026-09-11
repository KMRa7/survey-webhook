"""
アンケート配信サーバー（survey_webhook）
=========================================
- LINEの「友だち追加(follow)」「メッセージ／postback」イベントを受け取り、
  アンケート（Flexメッセージ）の送信・回答の記録を行います。
- タグ別セグメントへのテキスト配信（/broadcast）も提供します。

このリッチメニュー管理ツール本体・プロキシとは完全に別のサーバーです。
リッチメニュー機能には一切影響しません（このファイルを設置しなくてもリッチメニューは動作します）。

必要な環境変数:
  ENCRYPTION_KEY   richmenu_proxy と同じ Fernet 鍵（店舗トークンの復号に使用）
  SUPABASE_URL     例 https://xxxx.supabase.co
  SUPABASE_KEY     Supabaseの service_role キー
  PROXY_KEY        管理サイトと共有する合言葉（/broadcast の認証に使用。プロキシと同じでよい）

Supabaseに必要なテーブル（SQL Editorで実行。SURVEY_SETUP.md 参照）:
  surveys, survey_responses

LINEでの設定（各店舗のチャネルごと）:
  LINE Developers → Messaging API → Webhook URL に
  https://<このサーバー>/webhook/<store_id> を設定し、Webhookを有効化してください。
  store_id は管理ページの店舗URL等で確認できるID文字列です。

起動:
  pip install -r requirements.txt
  gunicorn -b 0.0.0.0:$PORT app:app
"""

import os
import json
import requests
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from cryptography.fernet import Fernet

ENCRYPTION_KEY = os.environ["ENCRYPTION_KEY"].encode()
SUPABASE_URL   = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
PROXY_KEY      = os.environ.get("PROXY_KEY", "")

fernet = Fernet(ENCRYPTION_KEY)
app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type", "X-Proxy-Key"])

SB = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
LINE_API = "https://api.line.me"


# ── Supabase ヘルパ ──────────────────────────────────────
def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB, params=params)
    r.raise_for_status()
    return r.json()

def sb_post(table, body, prefer="return=representation"):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers={**SB, "Prefer": prefer}, json=body)
    r.raise_for_status()
    return r.json() if r.content else None

def sb_patch(table, params, body):
    requests.patch(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB, params=params, json=body)

def get_token(store_id):
    rows = sb_get("store_tokens", {"store_id": f"eq.{store_id}", "select": "enc"})
    if not rows:
        return None
    return fernet.decrypt(rows[0]["enc"].encode()).decode()

def get_active_survey(store_id, trigger):
    rows = sb_get("surveys", {"store_id": f"eq.{store_id}", "trigger": f"eq.{trigger}",
                              "active": "eq.true", "select": "*", "limit": "1"})
    return rows[0] if rows else None

def get_survey(survey_id):
    rows = sb_get("surveys", {"id": f"eq.{survey_id}", "select": "*"})
    return rows[0] if rows else None


# ── Flex メッセージ構築 ───────────────────────────────────
def build_question_flex(survey, qindex):
    q = survey["questions"][qindex]
    body_contents = [{
        "type": "text", "text": f"{qindex+1}. {q.get('label','')}",
        "wrap": True, "weight": "bold", "size": "sm",
    }]
    if q["type"] == "choice":
        for opt in q.get("options", []):
            body_contents.append({
                "type": "button", "style": "primary", "color": "#1f6feb",
                "action": {
                    "type": "postback",
                    "label": opt[:20] or "選択",
                    "data": f"survey_answer:{survey['id']}:{qindex}:{opt}",
                    "displayText": opt,
                },
            })
    elif q["type"] == "rating":
        for i in range(1, 6):
            body_contents.append({
                "type": "button", "style": "secondary",
                "action": {
                    "type": "postback",
                    "label": "☆" * i,
                    "data": f"survey_answer:{survey['id']}:{qindex}:{i}",
                    "displayText": "☆" * i,
                },
            })
    else:  # text
        body_contents.append({
            "type": "text", "text": "（下のメッセージ欄から回答を入力してください）",
            "size": "xs", "color": "#999999", "wrap": True,
        })
    return {
        "type": "flex",
        "altText": survey.get("title", "アンケート"),
        "contents": {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical",
                      "contents": [{"type": "text", "text": survey.get("title", "アンケート"),
                                   "weight": "bold", "size": "md"}]},
            "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body_contents},
        },
    }


def line_reply(token, reply_token, messages):
    requests.post(f"{LINE_API}/v2/bot/message/reply",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                  json={"replyToken": reply_token, "messages": messages})

def line_push(token, user_id, messages):
    requests.post(f"{LINE_API}/v2/bot/message/push",
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                  json={"to": user_id, "messages": messages})

def line_multicast(token, user_ids, messages):
    for i in range(0, len(user_ids), 500):  # LINE multicast limit
        requests.post(f"{LINE_API}/v2/bot/message/multicast",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                      json={"to": user_ids[i:i+500], "messages": messages})


# ── 回答の保存 ───────────────────────────────────────────
def get_or_create_response(survey_id, user_id):
    rows = sb_get("survey_responses", {"survey_id": f"eq.{survey_id}", "line_user_id": f"eq.{user_id}",
                                       "select": "*", "limit": "1"})
    if rows:
        return rows[0]
    created = sb_post("survey_responses", {
        "survey_id": survey_id, "line_user_id": user_id, "answers": [], "tags": [],
    })
    return created[0] if created else None

def save_answer(survey_id, user_id, qindex, value):
    resp = get_or_create_response(survey_id, user_id)
    answers = resp.get("answers") or []
    while len(answers) <= qindex:
        answers.append("")
    answers[qindex] = str(value)
    sb_patch("survey_responses", {"id": f"eq.{resp['id']}"}, {"answers": answers})
    return resp["id"]


# ── Webhook ──────────────────────────────────────────────
@app.route("/webhook/<store_id>", methods=["POST"])
def webhook(store_id):
    body = request.get_json(silent=True) or {}
    token = get_token(store_id)
    if not token:
        return jsonify(ok=False, error="token not set"), 200  # 200 to avoid LINE retries

    for ev in body.get("events", []):
        etype = ev.get("type")
        user_id = (ev.get("source") or {}).get("userId")

        if etype == "follow":
            survey = get_active_survey(store_id, "welcome")
            if survey and survey.get("questions"):
                flex = build_question_flex(survey, 0)
                line_reply(token, ev["replyToken"], [flex])

        elif etype == "postback":
            data = ev.get("postback", {}).get("data", "")
            if data.startswith("survey_answer:"):
                _, survey_id, qidx, value = data.split(":", 3)
                qidx = int(qidx)
                save_answer(survey_id, user_id, qidx, value)
                survey = get_survey(survey_id)
                if survey and qidx + 1 < len(survey["questions"]):
                    flex = build_question_flex(survey, qidx + 1)
                    line_reply(token, ev["replyToken"], [flex])
                elif survey and survey.get("show_thanks", True):
                    line_reply(token, ev["replyToken"],
                              [{"type": "text", "text": "ご回答ありがとうございました！"}])

        elif etype == "message" and (ev.get("message") or {}).get("type") == "text":
            # 自由記述の回答として、進行中の未回答テキスト質問に紐づけたい場合はここで判定
            # (シンプル版: 直近に作成した未完了レスポンスの最初の空きtextスロットに入れる)
            text = ev["message"]["text"]
            active = get_active_survey(store_id, "welcome") or get_active_survey(store_id, "manual")
            if active:
                resp = get_or_create_response(active["id"], user_id)
                answers = resp.get("answers") or []
                qs = active.get("questions", [])
                target = None
                for i, q in enumerate(qs):
                    if q["type"] == "text" and (i >= len(answers) or not answers[i]):
                        target = i
                        break
                if target is not None:
                    save_answer(active["id"], user_id, target, text)
                    if target + 1 < len(qs):
                        line_reply(token, ev["replyToken"], [build_question_flex(active, target + 1)])
                    elif active.get("show_thanks", True):
                        line_reply(token, ev["replyToken"],
                                  [{"type": "text", "text": "ご回答ありがとうございました！"}])

    return jsonify(ok=True)


# ── タグ配信 ──────────────────────────────────────────────
@app.route("/broadcast", methods=["POST", "OPTIONS"])
def broadcast():
    if request.method == "OPTIONS":
        return ("", 204)
    if PROXY_KEY and request.headers.get("X-Proxy-Key") != PROXY_KEY:
        abort(401)
    body = request.get_json(force=True)
    store_id = body.get("store_id")
    tag = body.get("tag")
    message = body.get("message")
    if not (store_id and tag and message):
        return jsonify(error="store_id, tag, message required"), 400

    token = get_token(store_id)
    if not token:
        return jsonify(error="token not set for this store"), 400

    rows = sb_get("survey_responses", {"select": "line_user_id,tags"})
    user_ids = list({r["line_user_id"] for r in rows if tag in (r.get("tags") or [])})
    if not user_ids:
        return jsonify(ok=True, sent=0)

    line_multicast(token, user_ids, [{"type": "text", "text": message}])
    return jsonify(ok=True, sent=len(user_ids))


@app.route("/healthz")
def healthz():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
