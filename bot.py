import os
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Logging ────────────────────────────────────────────────────────────────
log = logging.getLogger("boinkers")
log.setLevel(logging.INFO)
h = logging.StreamHandler()
h.setFormatter(logging.Formatter("%(asctime)s [%(levelname).1s] %(message)s", "%H:%M:%S"))
log.handlers = [h]

# ── Config ─────────────────────────────────────────────────────────────────
BASE_URL = "https://boink.boinkers.co"
ACCOUNT_DELAY = int(os.getenv("ACCOUNT_DELAY", "15"))  # 15 sec between accounts
CYCLE_SLEEP = int(os.getenv("CYCLE_SLEEP", "60"))        # 60 min between cycles

DATA_FILE = Path(__file__).parent / "data.txt"       # initData per line
TOKENS_FILE = Path(__file__).parent / "tokens.json"  # cached JWT tokens

# ── Load .env ──────────────────────────────────────────────────────────────
def _load_dotenv():
    """Simple .env loader (no python-dotenv dependency needed)."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key not in os.environ:
            os.environ[key] = val

_load_dotenv()

# ── Telegram Notification ──────────────────────────────────────────────────
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TG_ENABLED   = bool(TG_BOT_TOKEN and TG_CHAT_ID)

def send_telegram(msg: str):
    """Send a message via Telegram bot. Non-blocking, silent on error."""
    if not TG_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_notification": False,
        }, timeout=10)
    except Exception:
        pass  # never crash on notification failure

# Version hash from /public/data/config — updated at startup
VERSION_HASH = "2037265378"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://boink.boinkers.co",
    "Referer": "https://boink.boinkers.co/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
}


# ═══════════════════════════════════════════════════════════════════════════
#  API Client
# ═══════════════════════════════════════════════════════════════════════════
class BoinkersAPI:
    def __init__(self, init_data: str, account_name: str):
        self.init_data = init_data.strip()
        self.name = account_name
        self.token = None
        self._load_token()

    # ── token persistence ────────────────────────────────────────────────
    def _load_token(self):
        if TOKENS_FILE.exists():
            tokens = json.loads(TOKENS_FILE.read_text())
            self.token = tokens.get(self.name)

    def _save_token(self):
        tokens = {}
        if TOKENS_FILE.exists():
            tokens = json.loads(TOKENS_FILE.read_text())
        tokens[self.name] = self.token
        TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

    # ── helpers ──────────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        """Append version params to URL."""
        sep = "&" if "?" in path else "?"
        return f"{BASE_URL}{path}{sep}p=unknown&v={VERSION_HASH}"

    def _auth_headers(self) -> dict:
        """Headers with auth token (lowercase authorization, no Bearer)."""
        h = {**HEADERS}
        if self.token:
            h["authorization"] = self.token
        return h

    def _post(self, path: str, data=None, auth=True) -> dict:
        headers = self._auth_headers() if auth else {**HEADERS}
        try:
            r = requests.post(self._url(path), json=data or {}, headers=headers, timeout=15)
            return self._handle(r)
        except Exception as e:
            log.error(f"[{self.name}] {path}: {e}")
            return {"ok": False, "error": str(e)}

    def _get(self, path: str, auth=True) -> dict:
        headers = self._auth_headers() if auth else {**HEADERS}
        try:
            r = requests.get(self._url(path), headers=headers, timeout=15)
            return self._handle(r)
        except Exception as e:
            log.error(f"[{self.name}] {path}: {e}")
            return {"ok": False, "error": str(e)}

    def _handle(self, r) -> dict:
        if r.status_code == 401:
            return {"ok": False, "error": "auth_expired", "status": 401}
        if r.status_code in (400, 403, 422):
            try:
                body = r.json()
                return {"ok": False, "error": body if isinstance(body, str) else body.get("message", str(body))}
            except Exception:
                return {"ok": False, "error": r.text[:200], "status": r.status_code}
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}", "status": r.status_code}
        try:
            data = r.json()
            # Unwrap nested response: {data: {...}}  → return data.data
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                return {"ok": True, "data": data["data"]}
            return {"ok": True, "data": data}
        except Exception:
            return {"ok": False, "error": "invalid_json"}

    # ── auth ─────────────────────────────────────────────────────────────
    def login(self) -> bool:
        """Login with initData → get JWT token."""
        url = f"{BASE_URL}/public/users/loginByTelegram?tgWebAppStartParam=boink1092680235&p=tdesktop"
        resp = requests.post(
            url,
            json={"initDataString": self.init_data, "tokenForSignUp": ""},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            self.token = data.get("token")
            if self.token:
                self._save_token()
                log.info(f"[{self.name}] ✅ Login OK — token saved")
                return True
        log.error(f"[{self.name}] ❌ Login failed: {resp.status_code} {resp.text[:150]}")
        return False

    def refresh_version_hash(self):
        """Update VERSION_HASH from server config."""
        global VERSION_HASH
        resp = requests.get(
            f"{BASE_URL}/public/data/config",
            headers={**HEADERS},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and "versionHash" in data:
                VERSION_HASH = data["versionHash"]
                log.info(f"Version hash updated: {VERSION_HASH}")
                return VERSION_HASH
        return VERSION_HASH

    # ── user ─────────────────────────────────────────────────────────────
    def get_user(self) -> dict:
        return self._get("/api/users/me")

    def get_config(self) -> dict:
        return self._get("/public/data/config")

    # ── booster ──────────────────────────────────────────────────────────
    def claim_booster(self, multiplier=2, option=1) -> dict:
        return self._post("/api/boinkers/addShitBooster", {
            "multiplier": multiplier,
            "optionNumber": option,
        })

    # ── spin ─────────────────────────────────────────────────────────────
    def spin(self, spin_type: str, amount: int) -> dict:
        """spin_type: 'slotMachine' or 'wheelOfFortune'"""
        return self._post(f"/api/play/spin{spin_type[0].upper() + spin_type[1:]}/{amount}")

    # ── upgrade ──────────────────────────────────────────────────────────
    def upgrade_boinker(self) -> dict:
        return self._post("/api/boinkers/upgradeBoinker/jetpack", {
            "isUpgradeCurrentBoinkerToMax": True,
        })
    
    def upgrade_equipment(self, equipment_type: str) -> dict:
        """Upgrade equipment: helmet, body, or jetpack"""
        return self._post(f"/api/boinkers/upgradeBoinker/{equipment_type}", {
            "isUpgradeCurrentBoinkerToMax": True,
        })

    # ── raffle ───────────────────────────────────────────────────────────
    def claim_raffle(self) -> dict:
        """Claim daily raffle ticket."""
        return self._post("/api/raffle/claimTicketForUser")

    # ── tasks ────────────────────────────────────────────────────────────
    def get_tasks(self) -> dict:
        return self._get("/api/rewardedActions/mine")

    def get_tasks_completed(self) -> dict:
        return self._get("/api/rewardedActions/getRewardedActionList")

    def click_task(self, name_id: str) -> dict:
        return self._post(f"/api/rewardedActions/rewardedActionClicked/{name_id}")

    def claim_task(self, name_id: str) -> dict:
        return self._post(f"/api/rewardedActions/claimRewardedAction/{name_id}")


# ═══════════════════════════════════════════════════════════════════════════
#  Account runner
# ═══════════════════════════════════════════════════════════════════════════
def run_account(api: BoinkersAPI) -> bool:
    """Run one cycle for an account. Returns True on success."""

    # Refresh version hash from server
    api.refresh_version_hash()

    # ── Login ───────────────────────────────────────────────────────────
    if not api.token or api.get_user()["ok"] is False:
        if not api.login():
            return False

    # ── User info ───────────────────────────────────────────────────────
    user_resp = api.get_user()
    if not user_resp["ok"]:
        log.error(f"[{api.name}] Cannot get user info: {user_resp.get('error')}")
        return False
    user = user_resp["data"]

    boink = user.get("boinkers", {})
    energy = user.get("gamesEnergy", {}).get("slotMachine", {})
    booster = boink.get("booster", {}).get("x2", {}) if boink.get("booster") else {}

    log.info(
        f"[{api.name}] "
        f"💰 {user.get('currencySoft', 0):,} coins | "
        f"💩 {user.get('currencyCrypto', 0):.2f} shit | "
        f"🎰 {energy.get('energy', 0)} spins | "
        f"📊 Lv{boink.get('currentBoinkerProgression', {}).get('level', '?')}"
    )

    # ── Booster x2 ─────────────────────────────────────────────────────
    last_claimed = booster.get("lastTimeFreeOptionClaimed")
    if last_claimed:
        last_dt = datetime.fromisoformat(last_claimed.replace("Z", "+00:00"))
        next_available = last_dt + timedelta(hours=2, minutes=5)
        now = datetime.now(timezone.utc)
        if now > next_available:
            b_resp = api.claim_booster(2, 3 if energy.get("energy", 0) > 30 else 1)
            if b_resp["ok"]:
                log.info(f"[{api.name}] 🔥 Booster x2 claimed!")
            else:
                log.warning(f"[{api.name}] Booster claim failed: {b_resp.get('error')}")
        else:
            wait_m = int((next_available - now).total_seconds() / 60)
            log.info(f"[{api.name}] ⏳ Next booster in ~{wait_m}m")
    else:
        b_resp = api.claim_booster(2, 1)
        if b_resp["ok"]:
            log.info(f"[{api.name}] 🔥 First booster claimed!")

    # ── Spin slots ─────────────────────────────────────────────────────
    spins = energy.get("energy", 0)
    if spins > 0:
        log.info(f"[{api.name}] 🎰 Spinning {spins}x...")
        amounts = [1000, 500, 150, 50, 25, 10, 5, 1]
        remaining = spins
        while remaining > 0:
            amt = next((a for a in amounts if a <= remaining), 1)
            s_resp = api.spin("slotMachine", amt)
            if s_resp["ok"]:
                d = s_resp["data"]
                prize = d.get("prize", {})
                log.info(
                    f"[{api.name}]   ↳ {d.get('outcome','?')} | "
                    f"{prize.get('prizeTypeName','?')}:{prize.get('prizeValue',0):,} | "
                    f"💰 {d.get('newDynamicCurrencies', {}).get('dc11', {}).get('balance', 0):,}"
                )
                remaining -= amt
            else:
                log.warning(f"[{api.name}] Spin failed: {s_resp.get('error')}")
                break
            time.sleep(1)

    # ── Tasks ───────────────────────────────────────────────────────────
    _do_tasks(api, user)

    # ── Sticker surprises ──────────────────────────────────────────────
    _do_sticker_surprises(api)

    # ── Raffle ticket ─────────────────────────────────────────────────
    _do_raffle(api)

    # ── Upgrade equipment & boinker ────────────────────────────────────
    max_attempts = 10
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        
        # Check current levels
        user_resp = api.get_user()
        if not user_resp["ok"]:
            log.warning(f"[{api.name}] Cannot refresh user info")
            break
        
        boinkers = user_resp.get("data", {}).get("boinkers", {})
        progression = boinkers.get("currentBoinkerProgression", {})
        part_levels = progression.get("partLevels", {})
        
        # Equipment yang belum Lv3
        equipment_to_upgrade = []
        for equipment in ["helmet", "body", "jetpack"]:
            level = part_levels.get(equipment, 0)
            if level < 3:
                equipment_to_upgrade.append((equipment, level))
        
        if not equipment_to_upgrade:
            # Semua equipment Lv3, upgrade Boinker
            log.info(f"[{api.name}] ⬆️ All equipment Lv3, upgrading Boinker...")
            up_resp = api.upgrade_boinker()
            if up_resp["ok"]:
                d = up_resp["data"]
                log.info(
                    f"[{api.name}] ⬆️ Boinker upgraded → Lv{d.get('rank', '?')} | "
                    f"💰 {d.get('newSoftCurrencyAmount', 0):,} | "
                    f"🎰 {d.get('newSlotMachineEnergy', 0)} spins"
                )
            else:
                err = up_resp.get("error", "")
                if "402" in str(err):
                    log.info(f"[{api.name}] 💤 Not enough resources to upgrade Boinker")
                else:
                    log.warning(f"[{api.name}] Boinker upgrade failed: {err}")
            break
        
        # Upgrade equipment yang belum Lv3
        log.info(f"[{api.name}] 🔧 Upgrade attempt {attempt}: {equipment_to_upgrade}")
        all_success = True
        for equipment, current_level in equipment_to_upgrade:
            up_resp = api.upgrade_equipment(equipment)
            if up_resp["ok"]:
                log.info(f"[{api.name}]   ✓ {equipment}: Lv{current_level} → Lv{current_level + 1}")
            else:
                err = up_resp.get("error", "")
                if "402" in str(err):
                    log.info(f"[{api.name}]   💤 {equipment}: Not enough resources")
                    all_success = False
                else:
                    log.warning(f"[{api.name}]   ✗ {equipment}: {err}")
                    all_success = False
            time.sleep(1)
        
        # Kalau ada yang gagal (resource habis), stop loop
        if not all_success:
            log.info(f"[{api.name}] ⏸️ Upgrade stopped: insufficient resources")
            break

    return True


def _do_raffle(api: BoinkersAPI):
    """Claim daily raffle ticket."""
    resp = api.claim_raffle()
    if resp["ok"]:
        log.info(f"[{api.name}] 🎟️ Raffle ticket claimed!")
    else:
        err = resp.get("error", "")
        if "already" in str(err).lower() or "claimed" in str(err).lower() or "177869" in str(err):
            log.info(f"[{api.name}] 🎟️ Raffle already claimed today")
        else:
            log.warning(f"[{api.name}] Raffle claim failed: {err}")


def _do_sticker_surprises(api: BoinkersAPI):
    """Claim sticker album surprise rewards."""
    tasks_resp = api.get_tasks()
    if not tasks_resp["ok"]:
        return

    # Find sticker surprise tasks
    tasks = tasks_resp.get("data", {})
    if isinstance(tasks, list):
        tasks = {t["nameId"]: t for t in tasks}

    sticker_tasks = {k: v for k, v in tasks.items() if k.startswith("Stiker_Surprize_")}
    if not sticker_tasks:
        return

    log.info(f"[{api.name}] 🎴 {len(sticker_tasks)} sticker surprise(s) available")

    for name_id, task in sticker_tasks.items():
        secs = task.get("secondsToAllowClaim", 10)

        # Click
        click_resp = api.click_task(name_id)
        if not click_resp["ok"]:
            log.warning(f"[{api.name}]   ✗ {name_id}: click failed")
            continue

        time.sleep(min(secs + 2, 15))

        # Claim
        claim_resp = api.claim_task(name_id)
        if claim_resp["ok"]:
            prize = claim_resp["data"].get("prizeGotten", "claimed")
            log.info(f"[{api.name}]   ✓ {name_id}: {prize}")
        else:
            err = claim_resp.get("error", "")
            if "already" in str(err).lower() or "claimed" in str(err).lower():
                log.info(f"[{api.name}]   ✓ {name_id}: already claimed")
            else:
                log.warning(f"[{api.name}]   ✗ {name_id}: claim failed — {err}")

        time.sleep(1)


def _do_tasks(api: BoinkersAPI, user: dict):
    """Process available rewarded tasks."""
    tasks_resp = api.get_tasks()
    completed_resp = api.get_tasks_completed()

    if not tasks_resp["ok"] or not completed_resp["ok"]:
        log.warning(f"[{api.name}] Cannot fetch tasks")
        return

    tasks = {k: v for k, v in tasks_resp["data"].items() if not v.get("claimDateTime")}
    completed = {i["nameId"]: i for i in completed_resp["data"]}

    todo = []
    for name_id, task in tasks.items():
        if name_id in completed and not task.get("claimDateTime"):
            todo.append({**completed[name_id], **task})

    # Filter: no ads, no verification, no stickers (handled separately)
    SKIP = {"sticker", "shareStory", "link", "watch-ad", "vip", "friends"}
    pending = [
        t for t in todo
        if not t.get("verification")
        and not t.get("claimDateTime")
        and t.get("type") not in SKIP
        and not t["nameId"].startswith("Stiker_Surprize_")
        and not t["nameId"].startswith("dailyVIP")
    ]

    if not pending:
        log.info(f"[{api.name}] ✅ No tasks to do")
        return

    log.info(f"[{api.name}] 📋 {len(pending)} tasks to do...")
    for task in pending:
        name_id = task["nameId"]
        secs = task.get("secondsToAllowClaim", 0)

        # Click task
        click_resp = api.click_task(name_id)
        if not click_resp["ok"]:
            log.warning(f"[{api.name}]   ✗ {name_id}: click failed")
            continue

        # Wait if needed
        if secs > 0:
            time.sleep(min(secs, 30))

        # Claim task
        claim_resp = api.claim_task(name_id)
        if claim_resp["ok"]:
            reward = claim_resp["data"].get("prizeGotten", "?")
            log.info(f"[{api.name}]   ✓ {name_id}: {reward}")
        else:
            log.warning(f"[{api.name}]   ✗ {name_id}: claim failed")

        time.sleep(1)


# ═══════════════════════════════════════════════════════════════════════════
#  Queue runner
# ═══════════════════════════════════════════════════════════════════════════
def run_queue(once=False):
    if not DATA_FILE.exists():
        log.error(f"data.txt not found at {DATA_FILE}")
        return

    accounts = [line.strip() for line in DATA_FILE.read_text().splitlines() if line.strip()]
    if not accounts:
        log.error("No accounts in data.txt")
        return

    n_accounts = len(accounts)
    log.info(f"🚀 Starting Boinkers bot — {n_accounts} account(s)")
    log.info(f"   Account delay: {ACCOUNT_DELAY}s | Cycle sleep: {CYCLE_SLEEP}m")
    if TG_ENABLED:
        log.info(f"   📱 Telegram notifications: ENABLED")

    cycle = 0
    while True:
        cycle += 1
        cycle_start = time.time()
        results = []
        account_reports = []

        for i, init_data in enumerate(accounts):
            name = f"akun{i+1}"
            api = BoinkersAPI(init_data, name)

            success = run_account(api)
            status = "✅" if success else "❌"
            results.append((name, success))
            log.info(f"[{name}] {status} Done")

            # Capture user data for report
            user_resp = api.get_user()
            username = "?"
            rank = 0
            soft_currency = 0
            shit = 0
            spins = 0
            
            if user_resp.get("ok"):
                user_data = user_resp.get("data", {})
                username = user_data.get("userName", "?")
                boinkers = user_data.get("boinkers", {})
                rank = boinkers.get("currentBoinkerProgression", {}).get("rank", 0)
                soft_currency = user_data.get("softCurrencyAmount", 0)
                dynamic_currencies = user_data.get("dynamicCurrencies", {})
                shit = dynamic_currencies.get("dc11", {}).get("balance", 0)
                spins = dynamic_currencies.get("dc1", {}).get("balance", 0)
            
            account_reports.append({
                "name": name,
                "username": username,
                "level": rank,
                "coins": soft_currency,
                "shit": shit,
                "spins": spins,
                "success": success,
            })

            # Delay between accounts (except last)
            if i < n_accounts - 1:
                log.info(f"⏳ Waiting {ACCOUNT_DELAY}s before next account...")
                time.sleep(ACCOUNT_DELAY)

        elapsed = time.time() - cycle_start
        ok = sum(1 for _, s in results if s)
        fail = n_accounts - ok

        # ── Send Telegram notification with detailed report ──────────────────────────────────
        if TG_ENABLED:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines = [
                "🤖 *Boinkers Bot Report*",
                "",
                f"📊 *Cycle:* {now_str}",
                "",
            ]
            
            for report in account_reports:
                lines.append(f"👤 *Account {report['name']}: {report['username']}*")
                lines.append(f"├─ 💰 Level: {report['level']}")
                lines.append(f"├─ 💵 Coins: {report['coins']:,}")
                lines.append(f"├─ 💩 Shit: {report['shit']:,.0f}")
                lines.append(f"├─ 🎰 Spins: {report['spins']}")
                lines.append(f"├─ ⬆️ Status: {'✅ Success' if report['success'] else '❌ Failed'}")
                lines.append(f"└─ ⏱ Elapsed: {elapsed:.0f}s")
                lines.append("")
            
            lines.append(f"📈 *Summary:* {ok}/{n_accounts} akun berhasil")
            if fail > 0:
                lines.append(f"⚠️ {fail} akun gagal")
            
            next_run = datetime.now(timezone.utc) + timedelta(minutes=CYCLE_SLEEP)
            lines.append(f"⏰ *Next cycle:* {next_run.strftime('%H:%M UTC')}")
            
            send_telegram("\n".join(lines))

        if once:
            log.info("✅ Single run complete (--once mode)")
            break

        next_run = datetime.now(timezone.utc) + timedelta(minutes=CYCLE_SLEEP)
        log.info(f"💤 Cycle complete — sleeping {CYCLE_SLEEP}m (next: {next_run.strftime('%H:%M UTC')})")
        time.sleep(CYCLE_SLEEP * 60)


if __name__ == "__main__":
    import sys
    once = "--once" in sys.argv
    run_queue(once=once)