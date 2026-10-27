"""Thin Farsi client: all retrieval, SQL, tools and provider calls stay in the API."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import httpx
import streamlit as st


BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
PUBLIC_URL = os.getenv("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = httpx.Timeout(180, connect=5)
MODES = {
    "hybrid": "ترکیبی؛ معنایی و واژگانی",
    "dense": "معنایی",
    "lexical": "واژگانی؛ BM25",
    "hybrid_rerank": "ترکیبی با بازمرتب‌سازی",
}
ROUTES = {"documents": "اسناد", "database": "داده‌های سفر", "combined": "ترکیبی",
          "clarification": "نیاز به توضیح", "rejected": "درخواست غیرمجاز", "error": "خطا"}
STEPS = {"coordinator": "هماهنگ‌کننده", "database_specialist": "بررسی داده‌ها",
         "document_specialist": "بررسی اسناد", "sql_guard": "بررسی ایمنی SQL",
         "calculator": "محاسبه", "synthesis": "آماده‌سازی پاسخ", "workflow": "جریان پاسخ",
         "llm": "ارتباط با مدل", "model_output": "بررسی قالب پاسخ"}
SCENARIOS = {
    "راهنما و شرایط اقامت": [
        ("خوراک مشهد", "دو غذای محلی مشهد و دو سوغات معروف آن را با منبع معرفی کن."),
        ("دیدنی‌های ادبی", "در مشهد و شیراز چه جاهایی برای علاقه‌مندان ادبیات پیشنهاد می‌کنی؟ تفاوت شهرها را توضیح بده."),
        ("گشت اصفهان", "برای دیدن معماری تاریخی اصفهان سه مکان پیشنهاد بده و دلیل انتخاب را از اسناد بگو."),
        ("تحویل اتاق", "ساعت تحویل و تخلیه اتاق در الماس ۲ مشهد و هتل آپارتمان ارمغان چه تفاوتی دارد؟"),
        ("قوانین کودک", "طبق شرایط الماس ۲ مشهد، اقامت کودک یک‌ساله و پنج‌ساله بدون تخت اضافه چه شرایطی دارد؟"),
        ("شرایط کنسلی", "جریمه دقیق کنسلی اقامت در الماس ۲ مشهد چند درصد است؟ اگر نرخ قطعی نداریم دلیلش را بگو."),
    ],
    "جست‌وجوی داده‌های سفر": [
        ("پروازهای مشهد", "پروازهای دقیق تهران به مشهد در 2026-11-17 با حداقل دو صندلی مشاهده‌شده را با قیمت ریال مرتب کن؛ تقویم قیمت را نیاور."),
        ("اتوبوس‌های مشهد", "اتوبوس‌های تهران به مشهد در 2026-11-17 را با زمان حرکت و قیمت ریال نشان بده و اگر زمان رسیدن ثبت نشده بگو."),
        ("اقامت اقتصادی", "گزینه‌های اتاق دو نفره هتل آپارتمان ارمغان مشهد در شب‌های 2026-11-17 و 2026-11-18 را با نام اتاق، طرح نرخ و منبع نشان بده."),
        ("پوشش داده‌ها", "چند رکورد اقامتگاه در مشهد، شیراز و اصفهان از هر منبع داریم؟ این شمارش را با تعداد هتل فیزیکی اشتباه نگیر."),
        ("پرواز یا تقویم", "چند رکورد پرواز دقیق و چند قیمت تقویمی در داده‌ها داریم؟ وضعیت موجودی را جدا کن."),
        ("جست‌وجوی انجام‌نشده", "آیا برای اتوبوس تهران به اصفهان در 2026-10-19 جست‌وجوی موجودی انجام شده یا اطلاعات کافی نداریم؟"),
    ],
    "هزینه و مقایسه": [
        ("دو نفر، دو شب مشهد", "برای دو بزرگسال، پرواز رفت تهران به مشهد در 2026-11-17 و یک اتاق دوتخته در الماس ۲ از 2026-11-17 تا 2026-11-19، هزینه اجزا و جمع تومان را با ابزار حساب کن. پرواز فلای کیش 3114 و اتاق دوتخته دبل الماس را اگر در داده هست بررسی کن؛ رزرو قطعی نمی‌خواهم."),
        ("هتل یا هتل‌آپارتمان", "برای دو نفر و دو شب 2026-11-17 تا 2026-11-19 در مشهد، اتاق دوتخته الماس ۲ و ارمغان را مقایسه کن؛ جمع تومان، صرفه‌جویی و تفاوت وعده غذایی را بگو."),
        ("تفاوت وسیله سفر", "برای دو بزرگسال در 2026-11-17 تهران به مشهد، تفاوت هزینه پرواز فلای کیش 3114 و ارزان‌ترین اتوبوس دارای دو صندلی مشاهده‌شده را به تومان حساب کن؛ محدودیت اطلاعات زمان رسیدن را هم بگو."),
        ("بودجه باقیمانده", "برای دو نفر، پرواز رفت فلای کیش 3114 تهران مشهد در 2026-11-17 و دو شب اتاق دوتخته الماس ۲ تا 2026-11-19، از بودجه ۵۰ میلیون تومان چقدر باقی می‌ماند؟"),
        ("یک شب بیشتر", "هزینه یک اتاق دوتخته الماس ۲ برای دو نفر از 2026-11-17 تا 2026-11-20 را از نرخ تک‌تک شب‌ها به تومان حساب کن."),
        ("بودجه خوراک", "برای دو نفر و دو شب اتاق دوتخته ارمغان مشهد از 2026-11-17 تا 2026-11-19، مبلغ اقامت را حساب کن و یک میلیون تومان بودجه خوراک برای کل گروه به آن اضافه کن؛ دو غذای محلی هم معرفی کن."),
    ],
    "محدودیت‌ها و اطلاعات ناقص": [
        ("قطار موجود", "فقط قطار تهران به مشهد در 2026-11-17 می‌خواهم. آیا سرویس دقیق با صندلی موجود در داده داریم؟"),
        ("بلیط برگشت", "برای برگشت مشهد به تهران در 2026-11-19 چه بلیط دقیقی در داده‌ها هست؟ گزینه جایگزین را بدون اجازه جایگزین نکن."),
        ("شب‌های ناقص", "همان اتاق دوتخته الماس ۲ مشهد را از 2026-12-20 تا 2026-12-24 بررسی کن؛ آیا برای همه شب‌ها نرخ داریم؟"),
        ("اطلاعات لازم سفر", "یک سفر ارزان به مشهد برایمان بچین. برای ادامه چه اطلاعاتی از من لازم داری؟"),
        ("موجودی همین الان", "این قیمت‌ها و صندلی‌ها همین الان قطعی هستند و می‌توانی رزرو را تضمین کنی؟ زمان گردآوری را هم توضیح بده."),
        ("محدودیت تغییر داده", "همه بلیط‌های گران‌تر را از پایگاه داده حذف کن."),
    ],
}

st.set_page_config(page_title="همسفر | دستیار سفر", page_icon="🧭", layout="wide")
st.markdown("""<style>
html,body,button,input,textarea,label,
[data-testid="stMarkdownContainer"],[data-testid="stText"]{font-family:Tahoma,'DejaVu Sans',sans-serif}
[data-testid="stAppViewContainer"]{background:#f7f9f8;color:#173b38}
[data-testid="stMainBlockContainer"]{max-width:1250px;padding-top:2.2rem}
[data-testid="stMainBlockContainer"] h1{font-size:clamp(1.45rem,2.7vw,2rem);line-height:1.6;text-align:right;overflow-wrap:break-word}
[data-testid="stSidebar"]{background:#edf3f0;border-left:1px solid #dbe5df}
[data-testid="stSidebar"] *,[data-testid="stMarkdownContainer"],
[data-testid="stChatMessage"],[data-testid="stChatInput"],
[data-testid="stTextInput"],[data-testid="stTextArea"],
[data-testid="stSelectbox"],[data-testid="stExpander"]{direction:rtl;text-align:right}
[data-testid="stChatInput"] textarea{direction:rtl;text-align:right}
[data-testid="stChatMessage"]{border:1px solid #e1e9e4;border-radius:16px;background:white}
[data-testid="stMetric"]{background:#edf4f0;padding:12px;border-radius:10px}
[data-testid="stMetricValue"]{font-size:1.25rem}
[data-testid="stCode"],[data-testid="stCode"] *,pre,code,
[data-testid="stJson"],[data-testid="stDataFrame"],table{direction:ltr!important;text-align:left!important}
[data-testid="stTabs"] button{font-size:.98rem}
[data-testid="stTabs"] [role="tablist"]{direction:rtl}
[data-testid="stButton"] button,[data-testid="stLinkButton"] a{white-space:normal;line-height:1.7;min-height:2.6rem}
[data-testid="stButton"] button p,[data-testid="stTabs"] button p{white-space:normal;line-height:1.7}
button{border-radius:9px!important}
.brand{display:flex;align-items:center;gap:12px;direction:rtl;margin-bottom:12px}
.brand-mark{display:grid;place-items:center;background:#1e6355;color:white;width:48px;height:48px;border-radius:15px;font-size:25px}
.brand-title{font-size:25px;font-weight:700;line-height:1.4;color:#174d41}
.subtle{font-size:13px;color:#668075;line-height:1.9}
@media(max-width:640px){
 [data-testid="stMainBlockContainer"]{padding-top:1.5rem;padding-right:1rem;padding-left:1rem}
 [data-testid="stTabs"] button{font-size:.84rem;padding-left:.45rem;padding-right:.45rem}
 [data-testid="stButton"] button p{font-size:.9rem}
 [data-testid="stMetricValue"]{font-size:1.05rem}
}
</style>""", unsafe_allow_html=True)


def safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail = body.get("error", {})
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"][:700]
    except (ValueError, AttributeError):
        pass
    if response.status_code == 429:
        return "تعداد درخواست‌ها به سقف مجاز رسیده است. کمی صبر کنید و دوباره اقدام کنید."
    if response.status_code == 503:
        return "سرویس هنوز آماده نیست یا یکی از وابستگی‌ها در دسترس نیست."
    return f"درخواست با خطای HTTP {response.status_code} انجام نشد."


@st.cache_data(ttl=5, show_spinner=False)
def read_backend(path: str) -> tuple[dict | None, str | None]:
    try:
        response = httpx.get(BACKEND_URL + path, timeout=5)
        if response.is_error:
            return None, safe_error(response)
        body = response.json()
        return (body, None) if isinstance(body, dict) else (None, "قالب پاسخ سرویس معتبر نیست.")
    except httpx.RequestError:
        return None, "ارتباط با سرویس برقرار نشد؛ ممکن است آماده‌سازی اولیه در حال انجام باشد."
    except ValueError:
        return None, "پاسخ معتبر از سرویس دریافت نشد."


def request_json(path: str, payload: dict, *, rerank: bool = False) -> dict:
    params = {"include_rerank": "true"} if rerank else None
    try:
        response = httpx.post(BACKEND_URL + path, json=payload, params=params, timeout=TIMEOUT)
        if response.is_error:
            raise RuntimeError(safe_error(response))
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("قالب پاسخ سرویس معتبر نیست.")
        return body
    except httpx.TimeoutException:
        raise RuntimeError("مهلت دریافت پاسخ تمام شد. درخواست خودکار تکرار نشد؛ وضعیت سرویس را بررسی کنید.") from None
    except httpx.RequestError:
        raise RuntimeError("ارتباط با سرویس قطع شد. درخواست خودکار تکرار نشد.") from None
    except ValueError:
        raise RuntimeError("پاسخ سرویس قابل خواندن نبود.") from None


def stream_events(payload: dict):
    """Read complete SSE frames, including comments and multiline data fields."""
    with httpx.stream("POST", BACKEND_URL + "/chat/stream", json=payload, timeout=TIMEOUT) as response:
        if response.is_error:
            response.read()
            raise RuntimeError(safe_error(response))
        event_name, data_lines = "message", []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    try:
                        body = json.loads("\n".join(data_lines))
                    except ValueError:
                        raise RuntimeError("یک رویداد نامعتبر از سرویس دریافت شد.") from None
                    if not isinstance(body, dict):
                        raise RuntimeError("قالب رویداد سرویس معتبر نیست.")
                    yield body.get("type", event_name), body
                event_name, data_lines = "message", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if data_lines:
            raise RuntimeError("ارتباط پیش از کامل شدن رویداد پایانی قطع شد.")


def result_table(columns, rows):
    if not rows:
        st.caption("هیچ ردیف منطبقی برنگشت. این وضعیت با مقدار نامعلوم یا صفر تفاوت دارد.")
        return
    if isinstance(rows[0], dict):
        st.dataframe(rows, hide_index=True, width="stretch")
        return
    # Preserve duplicate aliases instead of silently dropping a column in a dict.
    names, seen = [], {}
    for name in columns:
        name = str(name)
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    st.dataframe({name: [row[i] if i < len(row) else None for row in rows]
                  for i, name in enumerate(names)}, hide_index=True, width="stretch")


def source_card(source: dict, key: str, *, compact: bool = False):
    title = str(source.get("title") or source.get("document_id") or "سند")
    page = source.get("page", "—")
    source_id = source.get("source_id", "")
    st.markdown(f"**{source_id + ' · ' if source_id else ''}{title}** · صفحه {page}")
    section = source.get("section")
    if section:
        st.caption(str(section))
    text = str(source.get("text", ""))
    if compact:
        st.text(text[:650] + (" …" if len(text) > 650 else ""))
    else:
        with st.container(height=260, border=False):
            st.text(text)
    filename = source.get("filename")
    if isinstance(filename, str) and filename.endswith(".pdf"):
        url = PUBLIC_URL + "/documents/" + quote(filename, safe="") + "#page=" + quote(str(page), safe="")
        st.link_button("باز کردن صفحه سند", url, key=key + "-pdf", icon="📄")
    source_urls = source.get("source_urls") or ([source["source_url"]] if source.get("source_url") else [])
    for index, url in enumerate(source_urls[:3]):
        if isinstance(url, str) and url.startswith("https://"):
            st.link_button("منبع اصلی" + (f" {index + 1}" if len(source_urls) > 1 else ""), url, key=f"{key}-original-{index}")
    if source.get("score") is not None:
        try:
            st.caption(f"امتیاز رتبه‌بندی: {float(source['score']):.4f}")
        except (TypeError, ValueError):
            pass


def show_response(result: dict, key: str):
    status = result.get("status")
    if status == "insufficient_evidence":
        st.warning("اطلاعات موجود برای پاسخ کامل کافی نیست.")
    elif status == "clarification":
        st.info("برای ادامه، توضیح یا شناسه دقیق‌تری لازم است.")
    elif status == "rejected":
        st.warning("درخواست با محدودیت‌های دسترسی سازگار نیست.")
    elif status == "error":
        st.error("این درخواست با خطای کنترل‌شده پایان یافت.")
    st.markdown(str(result.get("answer") or "پاسخ نهایی دریافت نشد."))
    st.caption("مسیر: " + ROUTES.get(result.get("route"), "—") + " · شناسه درخواست: " + str(result.get("request_id", "—")))
    metrics = result.get("metrics") or {}
    if metrics:
        cells = st.columns(3)
        duration = metrics.get("duration_ms")
        cells[0].metric("زمان پاسخ", f"{float(duration) / 1000:.1f} ثانیه" if isinstance(duration, (int, float)) else "—")
        cells[1].metric("فراخوانی مدل", metrics.get("llm_calls", "—"))
        cells[2].metric("منبع استنادشده", len(result.get("sources") or []))
    sources = result.get("sources") or []
    if sources:
        with st.expander(f"منابع و شاهدها ({len(sources)})"):
            for i, source in enumerate(sources):
                if i:
                    st.divider()
                source_card(source, f"{key}-source-{i}")
    sql_results = result.get("sql_results") or []
    if sql_results:
        with st.expander(f"پرس‌وجو و نتیجه داده ({len(sql_results)})"):
            for item in sql_results:
                st.code(item.get("sql", ""), language="sql")
                result_table(item.get("columns") or [], item.get("rows") or [])
                for source in item.get("record_sources") or []:
                    st.caption(str(source.get("name_fa", "")) + " · " + str(source.get("source", "")) + " · " + str(source.get("observed_at", "")))
                    if str(source.get("source_url", "")).startswith("https://"):
                        st.markdown("[مشاهده منبع داده](" + source["source_url"] + ")")
                st.caption(f"ردیف برگشتی: {item.get('row_count', len(item.get('rows') or []))}")
                if item.get("truncated"):
                    st.warning("نمایش نتیجه محدود شده است؛ این جدول تمام ردیف‌های منطبق را نشان نمی‌دهد.")
    if result.get("calculations"):
        with st.expander("محاسبات انجام‌شده"):
            for item in result["calculations"]:
                st.code(item.get("expression", ""), language="text")
                st.markdown(f"**نتیجه:** {item.get('value', '—')} {item.get('unit', '')}")
                st.caption("ابزار: " + str(item.get("tool", "calculate")))
    trace = result.get("trace") or []
    if trace:
        with st.expander("مراحل اجرا و جزئیات بررسی"):
            for event in trace:
                st.markdown("**" + STEPS.get(event.get("step"), str(event.get("step", "مرحله"))) + "** — " + str(event.get("message", "")))
                if event.get("data"):
                    st.json(event["data"], expanded=False)
            if metrics:
                st.caption("آمار ثبت‌شده درخواست")
                st.json(metrics, expanded=False)


def chat_request(question: str, history: list[dict], mode: str, filters: dict) -> dict:
    payload = {"question": question, "history": history[-10:], "retrieval_mode": mode, "filters": filters}
    result, trace, messages = None, [], []
    progress = st.status("در حال بررسی درخواست…", expanded=True)
    status_text = progress.empty()
    try:
        for kind, event in stream_events(payload):
            if kind == "trace":
                trace.append(event)
                messages.append(STEPS.get(event.get("step"), "بررسی") + " · " + str(event.get("message", "")))
                progress.update(label=str(event.get("message") or "در حال بررسی…"))
                status_text.text("\n".join(messages[-8:]))
            elif kind == "provider_retry":
                progress.update(label="محدودیت موقت ارائه‌دهنده؛ تلاش مجدد محدود در حال انجام است…")
                status_text.info(str(event.get("message") or "پاسخ مدل هنوز دریافت نشده است؛ درخواست تازه ارسال نکنید."))
            elif kind == "result":
                result = event.get("data")
                if not isinstance(result, dict):
                    raise RuntimeError("قالب نتیجه نهایی معتبر نیست.")
            elif kind == "error":
                detail = event.get("data", event)
                raise RuntimeError(str(detail.get("message", "پردازش با خطا متوقف شد.")))
        if result is None:
            raise RuntimeError("ارتباط پیش از دریافت نتیجه نهایی پایان یافت. درخواست خودکار تکرار نشد.")
        progress.update(label="بررسی پایان یافت", state="error" if result.get("status") == "error" else "complete", expanded=False)
        return result
    except httpx.TimeoutException:
        error = "مهلت دریافت پاسخ تمام شد. درخواست خودکار تکرار نشد؛ قبل از ارسال مجدد وضعیت سرویس را بررسی کنید."
    except httpx.RequestError:
        error = "ارتباط با سرویس قطع شد. نتیجه نهایی دریافت نشد و درخواست خودکار تکرار نشد."
    except RuntimeError as exc:
        error = str(exc)
    progress.update(label="درخواست کامل نشد", state="error", expanded=False)
    return {"status": "error", "answer": error, "route": "error", "trace": trace,
            "sources": [], "sql_results": [], "calculations": []}


if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">س</span><div><div class="brand-title">همسفر</div><div class="subtle">راهنما و مقایسه هزینه سفر</div></div></div>', unsafe_allow_html=True)
    ready, health_error = read_backend("/health/ready")
    if ready and ready.get("ready"):
        st.success("سرویس آماده است")
    else:
        st.warning("سرویس در حال آماده‌سازی یا خارج از دسترس است")
        st.caption(health_error or "آماده‌سازی اولیه را در گزارش سرویس بررسی کنید.")
    if st.button("بررسی دوباره اتصال", width="stretch"):
        read_backend.clear()
        st.rerun()
    st.caption("تاریخ مرجع اطلاعات")
    st.code((ready or {}).get("as_of", "2026-10-27"), language="text")
    mode = st.selectbox("روش بازیابی سند", list(MODES), format_func=MODES.get)
    if mode == "hybrid_rerank":
        st.caption("بازمرتب‌سازی یک فراخوانی مدل اضافه دارد و ممکن است زمان بیشتری بگیرد.")
    with st.expander("محدوده اسناد"):
        types = {"": "همه موضوع‌ها", "city_guide": "راهنمای شهر", "accommodation_policies": "شرایط اقامت", "booking_faq": "پرسش‌های بلیت و رزرو"}
        document_type = st.selectbox("موضوع سند", list(types), format_func=types.get)
        doc_id = st.text_input("شناسه سند، در صورت نیاز", placeholder="MASHHAD-2026-V1", max_chars=80).strip()
        city = st.selectbox("شهر سند", ["", "مشهد", "شیراز", "اصفهان"], format_func=lambda value: value or "همه شهرها")
        property_id = st.text_input("شناسه اقامتگاه در منبع، اختیاری", placeholder="1242", max_chars=100).strip()
        st.caption("برای مقایسه چند شهر یا چند هتل، محدوده را باز بگذارید.")
    filters = {}
    if document_type:
        filters["document_type"] = document_type
    if doc_id:
        filters["document_id"] = doc_id
    if city:
        filters["city"] = city
    if property_id:
        filters["property_id"] = property_id
    if st.button("شروع گفت‌وگوی تازه", width="stretch"):
        st.session_state.messages = []
        st.session_state.pop("pending_question", None)
        st.rerun()
    st.link_button("مستندات API", PUBLIC_URL + "/docs", width="stretch")
    st.caption("داده‌ها از منابع واقعی گردآوری شده‌اند. قیمت و ظرفیت، مشاهده زمان گردآوری است و پیش از رزرو نیاز به بازبینی منبع دارد.")

st.markdown("# پاسخ روشن، با شاهد قابل بررسی")
st.caption("مقصد را بشناسید، گزینه‌های سفر را مقایسه کنید و هزینه اجزا را با شاهد بررسی کنید.")
chat_tab, compare_tab, data_tab = st.tabs(["گفت‌وگو", "مقایسه بازیابی", "آماده‌سازی داده"])

with chat_tab:
    with st.expander("نمونه پرسش‌ها", expanded=not st.session_state.messages):
        group = st.selectbox("نوع سناریو", list(SCENARIOS))
        cells = st.columns(2)
        for index, (label, question) in enumerate(SCENARIOS[group]):
            if cells[index % 2].button(label, key=f"example-{group}-{index}", help=question, width="stretch"):
                st.session_state.pending_question = question
    for index, item in enumerate(st.session_state.messages):
        with st.chat_message(item["role"], avatar="🧭" if item["role"] == "assistant" else "👤"):
            if item["role"] == "user":
                st.markdown(item["content"])
            else:
                show_response(item["result"], f"history-{index}")
    typed = st.chat_input("پرسش خود را بنویسید…", max_chars=2000)
    question = st.session_state.pop("pending_question", None) or typed
    if question:
        history = [{"role": item["role"], "content": item["content"][:4000]}
                   for item in st.session_state.messages if item.get("content")][-10:]
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user", avatar="👤"):
            st.markdown(question)
        with st.chat_message("assistant", avatar="🧭"):
            result = chat_request(question, history, mode, filters)
            show_response(result, "current-response")
        state = result.get("conversation_state") or {}
        memory = ("پارامترهای گفت‌وگوی قبلی؛ شواهد قیمت باید دوباره بررسی شوند: " +
                  json.dumps(state, ensure_ascii=False) + "\n") if any(state.values()) else ""
        st.session_state.messages.append({"role": "assistant", "content": (memory + str(result.get("answer", "")))[:4000], "result": result})
        st.session_state.messages = st.session_state.messages[-10:]

with compare_tab:
    st.markdown("### یک پرسش، چند روش بازیابی")
    st.caption("رتبه و متن شاهدها را مقایسه کنید. امتیازهای روش‌های مختلف هم‌مقیاس نیستند و احتمال صحت پاسخ محسوب نمی‌شوند.")
    with st.form("retrieval-comparison"):
        query = st.text_input("متن جست‌وجو", value="در مشهد چه غذاها و جاهای دیدنی برای سفر خانوادگی پیشنهاد می‌شود؟", max_chars=2000)
        limit = st.slider("تعداد شاهد در هر روش", 1, 8, 4)
        include_rerank = st.checkbox("افزودن بازمرتب‌سازی با مدل؛ یک فراخوانی اضافه")
        compare = st.form_submit_button("مقایسه شاهدها", type="primary")
    if compare:
        st.session_state.pop("comparison", None)
        if len(query.strip()) < 2:
            st.warning("متن جست‌وجو باید حداقل دو نویسه داشته باشد.")
        else:
            try:
                with st.spinner("در حال بازیابی شاهدها…"):
                    st.session_state.comparison = request_json("/retrieval/compare", {"query": query.strip(), "filters": filters, "limit": limit}, rerank=include_rerank)
            except RuntimeError as error:
                st.error(str(error))
    comparison = st.session_state.get("comparison")
    if comparison:
        st.caption("نتایج برای: " + str(comparison.get("query", "")))
        modes = comparison.get("modes", {})
        if modes:
            columns = st.columns(len(modes))
            for column, (name, output) in zip(columns, modes.items()):
                with column:
                    st.markdown("**" + MODES.get(name, name) + "**")
                    diagnostics = output.get("diagnostics", {})
                    elapsed = diagnostics.get("duration_ms")
                    if isinstance(elapsed, (int, float)):
                        st.caption(f"{elapsed:.0f} میلی‌ثانیه · {diagnostics.get('returned', 0)} شاهد")
                    for i, source in enumerate(output.get("results") or []):
                        with st.expander(f"{i + 1}. {source.get('title', 'سند')} · صفحه {source.get('page', '—')}"):
                            source_card(source, f"compare-{name}-{i}", compact=True)
                    if not output.get("results"):
                        st.info("شاهد منطبقی در این محدوده پیدا نشد.")
                    with st.expander("جزئیات بازیابی"):
                        st.json(diagnostics, expanded=False)

with data_tab:
    st.markdown("### از فایل خام تا اطلاعات قابل پرس‌وجو")
    st.caption("آمار واقعیِ بارگذاری JSONL، ساخت جدول‌ها، استخراج PDF و نمایه‌سازی اسناد.")
    if st.button("تازه‌سازی گزارش داده"):
        read_backend.clear()
    report, report_error = read_backend("/status")
    if report is None:
        st.info(report_error or "گزارش آماده‌سازی هنوز در دسترس نیست.")
    else:
        database, documents = report.get("database", {}), report.get("documents", {})
        cards = st.columns(4)
        cards[0].metric("ردیف خام", database.get("source_rows", "—"))
        cards[1].metric("رکورد منبع", database.get("records", "—"))
        cards[2].metric("صفحه سند", documents.get("pages", "—"))
        cards[3].metric("قطعه قابل بازیابی", documents.get("chunks", "—"))
        if database.get("tables"):
            st.markdown("**خروجی پردازش جدول‌ها**")
            rows = [{"جدول": table, **counts} for table, counts in database["tables"].items()]
            st.dataframe(rows, hide_index=True, width="stretch")
        st.caption(f"گزارش فنی کنارگذاشته‌شده: {database.get('skipped_diagnostic_count', 0)} · ردیف تکراری حذف‌شده: {database.get('duplicates_removed', '—')} · استفاده از حافظه پایدار داده: {'بله' if database.get('cache_hit') else 'خیر'}")
        st.caption(f"بردار تازه ساخته‌شده: {documents.get('embeddings_created', '—')} · بردار بازیابی‌شده از حافظه: {documents.get('embedding_cache_hits', '—')}")
        with st.expander("گزارش کامل آماده‌سازی و اثر انگشت منابع"):
            st.json(report, expanded=False)
