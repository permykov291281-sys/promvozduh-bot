"""ИИ-консультант ПромВоздух для сообщества ВКонтакте (учебный MVP, урок 4).

VK Long Poll -> история диалога -> нейросеть через Proxy API -> ответ клиенту.
Если в ответе есть блок [ЗАЯВКА]...[/ЗАЯВКА], он вырезается и уходит менеджеру.
Ключи читаются из .env рядом с файлом и никуда не выводятся.
"""
import json, os, random, re, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

VK_TOKEN = os.environ["VK_TOKEN"]
PROXYAPI_KEY = os.environ["PROXYAPI_KEY"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])
MODEL = os.environ.get("MODEL", "gpt-4.1")
MANAGER_IDS = [int(x) for x in os.environ.get("MANAGER_VK_IDS", "").split(",") if x.strip()]
HISTORY_LIMIT = 30  # сообщений в памяти на одного клиента

SYSTEM_PROMPT = (HERE / "system_prompt.md").read_text().replace(
    "{KNOWLEDGE_BASE}", (HERE / "knowledge_base.md").read_text())
HISTORY_FILE = HERE / "data" / "history.json"
LEADS_FILE = HERE / "data" / "zayavki.jsonl"
HISTORY_FILE.parent.mkdir(exist_ok=True)
history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
sent_leads = {}  # последняя отправленная заявка по каждому клиенту


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def http_post(url, data=None, headers=None, body=None, timeout=60, tries=4):
    """POST с повторами: связь сервера с VK иногда рвётся на TLS-рукопожатии."""
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"{urllib.parse.urlsplit(url).netloc}: {e}")
            time.sleep(1 + attempt * 2)


def vk(method, **params):
    params.update(access_token=VK_TOKEN, v="5.199")
    res = http_post(f"https://api.vk.com/method/{method}", params, timeout=20)
    if "error" in res:
        raise RuntimeError(f"VK {method}: {res['error'].get('error_msg')}")
    return res["response"]


def send(peer_id, text):
    vk("messages.send", peer_id=peer_id, message=text, random_id=random.randint(1, 2**31))


def ask_ai(messages):
    res = http_post(
        "https://api.proxyapi.ru/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {PROXYAPI_KEY}", "Content-Type": "application/json"},
        body=json.dumps({"model": MODEL, "temperature": 0.3,
                         "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages}).encode(),
        timeout=90)
    return res["choices"][0]["message"]["content"]


def clean(text):
    """Убрать Markdown, который VK показывает как мусор."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[\*•]\s+", "— ", text, flags=re.M)
    return text.replace("`", "").strip()


def user_name(uid):
    try:
        u = vk("users.get", user_ids=uid)[0]
        return f"{u['first_name']} {u['last_name']}"
    except Exception:
        return str(uid)


def notify_managers(uid, lead):
    record = {"time": datetime.now().isoformat(timespec="seconds"), "vk_id": uid, "lead": lead}
    with LEADS_FILE.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    text = (f"Новая заявка из VK-бота ПромВоздух\n"
            f"Клиент: {user_name(uid)} — https://vk.com/id{uid}\n\n{lead}")
    for mid in MANAGER_IDS:
        try:
            send(mid, text)
        except Exception as e:
            log("не удалось уведомить менеджера", mid, e)
    log("заявка от", uid, "уведомлено менеджеров:", len(MANAGER_IDS))


def handle(uid, text):
    key = str(uid)
    if text.strip().lower() in ("/reset", "/сброс"):
        history.pop(key, None)
        send(uid, "История диалога очищена.")
        return
    if text.strip().lower().lstrip("/") in ("id", "ид", "айди"):
        send(uid, f"Ваш VK ID: {uid}")
        return
    msgs = history.setdefault(key, [])
    msgs.append({"role": "user", "content": text})
    try:
        vk("messages.setActivity", peer_id=uid, type="typing")  # «печатает…»
    except Exception:
        pass
    try:
        answer = ask_ai(msgs[-HISTORY_LIMIT:])
    except Exception as e:
        log("ошибка нейросети:", e)
        msgs.pop()
        send(uid, "Извините, у меня временная техническая заминка. Напишите, пожалуйста, ещё раз через минуту.")
        return
    lead = re.search(r"\[ЗАЯВКА\](.*?)\[/ЗАЯВКА\]", answer, re.S)
    visible = clean(re.sub(r"\[ЗАЯВКА\].*?(\[/ЗАЯВКА\]|$)", "", answer, flags=re.S))
    msgs.append({"role": "assistant", "content": answer})
    history[key] = msgs[-HISTORY_LIMIT:]
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False))
    send(uid, visible or "Спасибо! Передал информацию менеджеру.")
    if lead:
        lead_text = lead.group(1).strip()
        if visible.rstrip().endswith("?"):
            log("заявка пропущена: бот ещё ждёт подтверждения", uid)  # модель поспешила
        elif sent_leads.get(key) == lead_text:
            log("заявка пропущена: дубль", uid)
        else:
            sent_leads[key] = lead_text
            notify_managers(uid, lead_text)


def catch_up():
    """После перезапуска ответить тем, чьё последнее сообщение осталось без ответа."""
    try:
        convs = vk("messages.getConversations", group_id=GROUP_ID, filter="unanswered", count=20)["items"]
    except Exception as e:
        log("catch_up:", e); return
    for c in convs:
        uid = c["conversation"]["peer"]["id"]
        if uid <= 0 or time.time() - c["last_message"]["date"] > 3600:
            continue
        items = vk("messages.getHistory", peer_id=uid, group_id=GROUP_ID, count=20)["items"]
        unanswered = []
        for m in items:  # от новых к старым, до последнего ответа бота
            if m["out"]:
                break
            if m.get("text"):
                unanswered.insert(0, m["text"])
        if unanswered:
            log("догоняю", uid, ":", " / ".join(unanswered)[:80])
            safe_handle(uid, "\n".join(unanswered))


def safe_handle(uid, text):
    try:
        handle(uid, text)
    except Exception as e:
        log("ошибка обработки", uid, ":", e)
        try:
            send(uid, "Извините, у меня техническая заминка со связью. Напишите, пожалуйста, ещё раз.")
        except Exception:
            pass


def main():
    log(f"бот запущен: сообщество {GROUP_ID}, модель {MODEL}, менеджеров {len(MANAGER_IDS)}")
    catch_up()
    srv, ts = None, None
    while True:
        try:
            if srv is None:
                srv = vk("groups.getLongPollServer", group_id=GROUP_ID)
                ts = ts or srv["ts"]  # старый ts сохраняем, чтобы не потерять сообщения
            res = http_post(srv["server"], {"act": "a_check", "key": srv["key"], "ts": ts, "wait": 25}, timeout=35)
            if "failed" in res:
                if res["failed"] == 1:
                    ts = res["ts"]
                else:
                    srv = None
                    if res["failed"] == 3:
                        ts = None
                continue
            ts = res["ts"]
            for ev in res.get("updates", []):
                if ev["type"] != "message_new":
                    continue
                m = ev["object"]["message"]
                if m.get("from_id", 0) > 0 and m.get("text"):
                    log("сообщение от", m["from_id"], ":", m["text"][:80])
                    safe_handle(m["from_id"], m["text"])
        except Exception as e:
            log("переподключение:", e)
            srv = None
            time.sleep(2)


if __name__ == "__main__":
    main()
