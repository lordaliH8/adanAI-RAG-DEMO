#!/usr/bin/env python3
"""Rebuild the five source-derived Persian PDFs from pinned, local source receipts.

This is an offline authoring utility, never an application ingestion path. No LLM,
provider calls, inventory crawling, or answer fixtures are involved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "tools/data-authoring/sources"
COMPILED_AT = "2026-10-27"


def load(name):
    return json.loads((SOURCES / f"{name}.json").read_text())


def paragraph(text):
    return {"type": "paragraph", "text": re.sub(r"\[\s*(?:یادداشت\s*)?[\d۰-۹]+\s*\]", "", text).strip()}


def passages(name, indexes):
    source = load(name)
    return [paragraph(source["sections"][i]["text"]) for i in indexes]


def chapter(code, title, blocks, source, property_id=None):
    return {"code": code, "title": title, "blocks": blocks, "source": source,
            "property_id": property_id}


def table(headers, rows):
    return {"type": "table", "headers": headers, "rows": rows}


def note(text):
    return {"type": "callout", "text": text}


def source_info(name):
    d = load(name)
    return {k: d[k] for k in ["title", "source_url", "revision_url", "retrieved_at", "html_sha256", "license_urls"]}


def city_document(code, filename, title, city, chapters, names):
    return {"id": code, "filename": filename, "title": title, "city": city,
            "document_type": "city_guide", "source": "wikimedia", "compiled_at": COMPILED_AT,
            "sources": [source_info(name) for name in names], "chapters": chapters,
            "notice": "گزیده موضوعی و ویرایش‌شده از متن آزاد ویکی‌سفر و ویکی‌پدیا؛ بدون نرخ‌ها و برنامه‌های زمانی قدیمی. این گردآوری، بروشور رسمی نهاد گردشگری نیست.",
            "license": "CC BY-SA 4.0", "property_ids": []}


def documents():
    mashhad = city_document("MASHHAD-2026-V1", "mashhad-culture.pdf", "مشهد؛ میراث، خوراک و پیرامون شهر", "مشهد", [
        chapter("MASH-01", "شهر و تاریخ", [paragraph("مشهد مرکز استان خراسان رضوی در شمال شرقی ایران است. در دوره افشاریان، نادرشاه این شهر را به پایتختی برگزید. حرم امام رضا از مهم‌ترین کانون‌های تاریخی و زیارتی شهر است."), *passages("mashhad", [5, 6])], "mashhad"),
        chapter("MASH-02", "توس و آرامگاه فردوسی", [*passages("ferdowsi", [0, 1, 2, 3]), note("توس، محل آرامگاه فردوسی است؛ روستای پاژ که زادگاه او معرفی می‌شود، با محوطه آرامگاه یکی نیست. این بازدید در پیرامون مشهد قرار می‌گیرد.")], "ferdowsi"),
        chapter("MASH-03", "معماری و روایت‌های شاهنامه", passages("ferdowsi", [20, 21, 22]), "ferdowsi"),
        chapter("MASH-04", "موزه‌ها و یادمان‌های شهر", [*passages("mashhad", [71]), table(["مجموعه", "شناسه فرهنگی یا تاریخی"], [["موزه‌های آستان قدس رضوی", "فرش، سکه و تمبر، نقاشی و مجموعه‌های دیگر"], ["موزه نادری و آرامگاه نادرشاه", "یادمان نادرشاه افشار"], ["موزه توس", "موزه فردوسی در مجموعه توس"], ["مسجد گوهرشاد", "از بناهای تاریخی مشهد"], ["گنبد خشتی و مدرسه عباسقلیخان", "دیگر بناهای تاریخی نام‌برده در راهنمای شهر"]]), paragraph("در فهرست موزه‌های ویکی‌سفر، موزه بزرگ تاریخ طبیعی خراسان، موزه علوم و تاریخ طبیعی مشهد و موزه آب نیز نام برده شده‌اند. نام‌بردن از یک موزه در این فهرست، به معنای تأیید ساعت بازدید یا بازبودن آن در روز سفر نیست.")], "mashhad"),
        chapter("MASH-05", "فضای باز، خوراک و سوغات", [table(["نوع گردش", "مکان‌های معرفی‌شده"], [["بوستان شهری", "کوهسنگی، وکیل‌آباد، پارک ملت"], ["گردش در دامنه و طبیعت", "کوهستان پارک خورشید در انتهای بلوار هاشمیه، بوستان هفت‌حوض"], ["پیرامون مشهد", "طرقبه، شاندیز، چالیدره و سد گلستان"]]), *passages("mashhad", [150, 127, 128]), note("شله مشهدی با شیشلیک یک غذا نیست. شله در متن منبع به‌عنوان غذای ویژه مشهد و مرتبط با برخی مراسم مذهبی معرفی شده است؛ شیشلیک، چلوگوشت، کباب و دیزی نیز در معرفی خوراک شهر و اطراف آن آمده‌اند.")], "mashhad"),
    ], ["mashhad", "ferdowsi"])

    shiraz = city_document("SHIRAZ-2026-V1", "shiraz-culture.pdf", "شیراز؛ باغ‌ها، شعر و میراث شهری", "شیراز", [
        chapter("SHIR-01", "شهر باغ‌ها و شاعران", [paragraph("شیراز مرکز استان فارس است. آرامگاه حافظ و سعدی، باغ‌های تاریخی و مجموعه زندیه از محورهای اصلی شناخت این شهر هستند."), table(["گروه", "مکان‌های نام‌برده در ویکی‌سفر"], [["شعر و آرامگاه", "حافظیه، سعدیه، آرامگاه خواجوی کرمانی"], ["باغ", "ارم، عفیف‌آباد، دلگشا، جهان‌نما، نارنجستان قوام"], ["بافت تاریخی", "ارگ کریم‌خان، بازار وکیل، حمام وکیل، مسجد وکیل، سرای مشیر"], ["دیگر بناها", "مسجد نصیرالملک، شاهچراغ، دروازه قرآن"]]), paragraph("موزه پارس در نزدیکی بازار وکیل، موزه نارنجستان قوام، موزه نظامی در باغ عفیف‌آباد و موزه هفت‌تنان از موزه‌های معرفی‌شده در راهنمای شهر هستند.")], "shiraz"),
        chapter("SHIR-02", "حافظیه؛ تاریخ و جایگاه", passages("hafezieh", [0, 1, 2]), "hafezieh"),
        chapter("SHIR-03", "حافظیه؛ معماری باغ و آرامگاه", passages("hafezieh", [7, 8, 9, 10, 13]), "hafezieh"),
        chapter("SHIR-04", "باغ ارم؛ بنا و محوطه", [*passages("eram", [0, 1, 5, 6, 8, 9, 10, 11, 12]), note("در منبع، زمان ساخت و بنیان‌گذار اولیه باغ ارم قطعی دانسته نشده است. وجود عمارت قاجاری به معنای قطعیت تاریخ آغاز خود باغ نیست.")], "eram"),
        chapter("SHIR-05", "خوراک، موزه و گردش پیرامونی", [paragraph("فالوده شیرازی، کوفته سبزی و آش سبزی از خوراک‌هایی هستند که در ویکی‌سفر برای تجربه در شیراز نام برده شده‌اند."), *passages("shiraz", [36, 56]), table(["مقصد", "جایگاه در راهنمای سفر"], [["حافظیه، سعدیه، باغ ارم و مجموعه زندیه", "دیدنی‌های شهری شیراز"], ["تخت جمشید و نقش رستم", "مقاصد پیرامونی استان فارس؛ با دیدنی‌های درون شهر یکی نیستند"], ["پاسارگاد و آرامگاه کورش", "مقصد میراثی پیرامونی؛ نیازمند برنامه جداگانه"], ["دریاچه مهارلو", "در راهنمای شهر با نام دریاچه نمک نیز آمده است"]]), note("فهرست خوراک و دیدنی‌ها، شامل قیمت غذا، بهای ورودی، ساعت کار، زمان رانندگی یا هزینه رفت‌وبرگشت نیست. این مقادیر در این گزیده ثبت نشده‌اند.")], "shiraz"),
    ], ["shiraz", "hafezieh", "eram"])

    isfahan = city_document("ISFAHAN-2026-V1", "isfahan-culture.pdf", "اصفهان؛ میدان، پل و خوراک محلی", "اصفهان", [
        chapter("ISF-01", "نقش جهان و بازار", [*passages("isfahan", [24, 25, 26]), paragraph("چهارباغ عباسی، چهارباغ خواجو، میدان نقش جهان و میدان کهنه نام‌های متفاوتی هستند. میدان نقش جهان در منبع با نام‌های میدان شاه و میدان امام نیز آمده است؛ میدان کهنه با سبزه‌میدان و میدان امام علی شناخته می‌شود.")], "isfahan"),
        chapter("ISF-02", "مسجدها، مدرسه‌ها و کاخ‌ها", [*passages("isfahan", [29, 30, 31, 34, 35, 36]), paragraph("عالی‌قاپو بنایی شش‌طبقه در مجموعه میدان نقش جهان است و طبقه بالای آن تالار موسیقی است. چهل‌ستون در دوره صفوی کارکرد کاخ تشریفات داشته و در خیابان استانداری قرار دارد. تالار اشرف نیز در محدوده خیابان سپه و استانداری معرفی شده است."), *passages("isfahan", [40])], "isfahan"),
        chapter("ISF-03", "پل‌های زاینده‌رود و فضای باز", [*passages("isfahan", [43, 44, 45, 46]), paragraph("مارنان نیز در فهرست پل‌های اصفهان آمده است. وجود پل‌های تاریخی، تضمینی درباره جاری‌بودن آب زاینده‌رود در روز سفر نیست."), *passages("isfahan", [65, 68, 70, 71, 72]), paragraph("ناژوان در غرب اصفهان و در حاشیه زاینده‌رود معرفی شده است. باغ پرندگان و باغ خزندگان از دیگر مکان‌های نام‌برده در راهنمای شهر هستند.")], "isfahan"),
        chapter("ISF-04", "جلفا و کلیساها", [*passages("isfahan", [48]), paragraph(load("isfahan")["sections"][49]["text"].split("در ورودی این کلیسا")[0]), paragraph(load("isfahan")["sections"][50]["text"].split("در ورودی این کلیسا")[0]), *passages("isfahan", [51])], "isfahan"),
        chapter("ISF-05", "بریان، خورش ماست و صنایع دستی", [paragraph("بریان و خورش ماست در ویکی‌سفر به‌عنوان خوراک‌های محلی اصفهان معرفی شده‌اند. بریان از گوشت گوسفند تهیه می‌شود و به محل عرضه آن بریانی گفته می‌شود."), *passages("isfahan", [104, 108]), paragraph("خورش ماست در این معرفی، خوراکی سرد و حلیم‌مانند است که امروزه به‌عنوان دسر هم مصرف می‌شود. نام آن نباید با خورش‌های گرمِ معمول یکی فرض شود."), table(["گروه سوغات", "نمونه‌های نام‌برده"], [["شیرینی", "گز اصفهان"], ["پارچه و نقش", "سفره قلمکار، فرش و مینیاتور"], ["هنرهای چوب و فلز", "خاتم‌کاری، منبت، قلم‌زنی، میناکاری و صنایع مسی"]]), paragraph("راهنمای شهر، مسیر بازار بزرگ تا سردر قیصریه در نقش جهان و قدم‌زدن در چهارباغ را نیز در میان تجربه‌های شهری پیشنهاد کرده است. این معرفی، حاوی نرخ فروش یا موجودی فروشگاه‌های صنایع دستی نیست.")], "isfahan"),
    ], ["isfahan"])

    captured = load("captured-profiles")
    hotels = {r["source_id"]: r for r in captured if r["source"] == "eghamat24"}
    ali = next(r for r in captured if r["entity_type"] == "property" and r["source"] == "alibaba")
    stay_chapters = []
    for n, hid in enumerate(["1242", "106"], 1):
        r = hotels[hid]; d = r["data"]
        blocks = [paragraph(d["description"]), table(["مشخصه", "اطلاعات ثبت‌شده"], [["نشانی", d["address"]], ["ورود", d["check_in"][:5]], ["خروج", d["check_out"][:5]]])]
        for rule in d["rules"]:
            blocks += [{"type": "heading", "text": rule["title"]}, paragraph(rule["description"])]
        stay_chapters.append(chapter(f"STAY-0{n}", r["name_fa"], blocks, "eghamat24", hid))
    # These are short attributed factual notes, not a wholesale commercial article.
    stay_chapters.append(chapter("STAY-03", "خدمات و محدودیت‌های الماس ۲ در معرفی علی‌بابا", [
        paragraph("در معرفی ثبت‌شده علی‌بابا برای هتل الماس ۲ مشهد، صبحانه بوفه در طبقه هفدهم، اینترنت نامحدود رایگان در لابی و اتاق‌ها، و ترانسفر استقبال و بدرقه فرودگاهی رایگان ذکر شده است. پذیرایی صبحانه در اتاق به‌صورت روم‌سرویس، هزینه صبحانه و پذیرایی دارد."),
        paragraph("ترانسفر فرودگاهی و راه‌آهن از ساعت ۰۰:۰۰ تا ۰۶:۰۰ انجام نمی‌شود. رایگان‌بودن ترانسفر به معنای ارائه آن در تمام ساعات شبانه‌روز نیست. ترانسفر از هتل به حرم نیز در این معرفی رایگان اعلام شده است."),
        paragraph("در توضیح تفصیلی همان صفحه، استفاده از مجموعه آبی شامل استخر، سونا و جکوزی برای مهمانان هتل روزانه دو ساعت رایگان ذکر شده است. خدمات روم‌سرویس رایگان به همه اتاق‌ها تعمیم داده نشده و برای هانی‌مون، هانی‌مون رویال و پرزیدنت‌های ایران باستان، صفویه و ترکیه نام برده شده است."),
        paragraph("دبل فیروزه، سه‌تخته یاقوت، هانی‌مون، هانی‌مون رویال و کانکت یاقوت در فهرست اتاق‌های بدون ویو و با شیشه مشجر آمده‌اند. سوئیت‌های هانی‌مون و ماه‌عسل و سوئیت‌های پرزیدنت، فاقد اتاق‌خواب جداگانه معرفی شده‌اند."),
        paragraph("برای پکیج تولد یا سالگرد ازدواج، هماهنگی تلفنی با رزرواسیون هتل از ۲۴ ساعت پیش از ورود لازم دانسته شده است. دیزاین میز، کیک و کارت تبریک در معرفی این پکیج آمده‌اند."),
        note("این اطلاعات از معرفی همین هتل در علی‌بابا و در تاریخ ثبت منبع است. شرایط نهایی اتاق، طرح رزرو و تأیید هتل باید جداگانه بررسی شود؛ مشخصات الماس ۱ یا الماس نوین جایگزین مشخصات الماس ۲ نیست.")], "alibaba", "1242"))
    stay_sources = [{"title": r["name_fa"], "source_url": r["source_url"], "provider": r["source"], "property_id": "1242" if r["source"] == "alibaba" else r["source_id"], "provider_property_id": r["source_id"], "observed_at": r["observed_at"], "receipt": r["evidence"]} for r in [hotels["1242"], hotels["106"], ali]]
    stay = {"id": "STAY-2026-V1", "filename": "mashhad-stay-conditions.pdf", "title": "مشهد؛ معرفی و شرایط اقامت دو هتل", "city": "مشهد", "document_type": "accommodation_policies", "source": "mixed", "compiled_at": COMPILED_AT, "sources": stay_sources, "chapters": stay_chapters, "notice": "یادداشت‌های مستند از معرفی اقامت۲۴ و علی‌بابا؛ ثبت منبع: 2026-10-18. این گردآوری تأیید رزرو یا تضمین خدمات امروز نیست.", "license": "Attributed factual compilation; commercial source rights not asserted", "property_ids": ["1242", "106"]}

    faqs = [r for r in captured if r["entity_type"] == "content"]
    faq_chapters = []
    for number, (mode, label) in enumerate([("flight", "پرسش‌های پرواز"), ("bus", "پرسش‌های اتوبوس")], 1):
        blocks = []
        for r in faqs:
            if r["data"]["mode"] != mode: continue
            blocks.append({"type": "heading", "text": r["name_fa"]})
            text = r["data"]["text"]
            if "استرداد" in r["name_fa"]:
                text = "در راهنمای علی‌بابا امکان استرداد بلیت مطرح شده است، اما میزان جریمه به نوع بلیت، کلاس پروازی و کلاس نرخی بستگی دارد. متن راهنما از مسافر می‌خواهد قوانین استرداد همان بلیت را هنگام رزرو بخواند. این پرسش عمومی، درصد جریمه مشخصی برای یک پرواز تعیین نمی‌کند."
            elif "هزینه بیشتری" in r["name_fa"]:
                text = "راهنمای علی‌بابا برای خرید آنلاین هزینه بیشتر نسبت به خرید حضوری اعلام نمی‌کند و به امکان تخفیف و پشتیبانی اشاره دارد. این بیان عمومی، مقایسه قیمت یک بلیت مشخص در دو فروشنده نیست."
            elif "نحوه رزرو" in r["name_fa"]:
                text = "در راهنمای خرید اتوبوس علی‌بابا، ابتدا مبدأ، مقصد، تاریخ حرکت و تعداد بلیت انتخاب می‌شود. سپس گزینه‌های موجود همان تاریخ نمایش داده می‌شوند و خرید با کارت بانکی انجام می‌شود."
            blocks.append(paragraph(text))
        blocks.append(note("این‌ها پاسخ‌های عمومی ثبت‌شده در وب‌سایت علی‌بابا هستند. شرایط مشخصِ بلیت، کلاس نرخی، شرکت حمل‌ونقل و صفحه خرید بر این توضیح عمومی مقدم است. این متن شامل موجودی یا نرخ روز نیست."))
        ch = chapter(f"FAQ-0{number}", label, blocks, "alibaba")
        ch["source_title"] = "پرسش‌های متداول پرواز علی‌بابا" if mode == "flight" else "پرسش‌های متداول اتوبوس علی‌بابا"
        faq_chapters.append(ch)
    faq = {"id": "FAQ-2026-V1", "filename": "booking-faq.pdf", "title": "پرسش‌های متداول رزرو پرواز و اتوبوس", "city": None, "document_type": "booking_faq", "source": "alibaba", "compiled_at": COMPILED_AT, "sources": [{"title": "پرسش‌های متداول علی‌بابا", "source_url": u, "provider": "alibaba", "observed_at": min(r["observed_at"] for r in faqs)} for u in sorted({r["source_url"] for r in faqs})], "chapters": faq_chapters, "notice": "گزیده و یادداشت مستند از ده پرسش واقعی علی‌بابا؛ ثبت منبع: 2026-10-18. توضیح عمومی جای شرایط همان بلیت را نمی‌گیرد.", "license": "Attributed factual compilation; commercial source rights not asserted", "property_ids": []}
    result = [mashhad, shiraz, isfahan, stay, faq]
    for source in faq["sources"]:
        source["title"] = "پرسش‌های متداول اتوبوس علی‌بابا" if "bus-ticket" in source["source_url"] else "پرسش‌های متداول پرواز علی‌بابا"
    for doc in result:
        for ch in doc["chapters"]:
            if ch["source"] in {"mashhad", "shiraz", "isfahan", "ferdowsi", "hafezieh", "eram"}:
                ch["source_title"] = load(ch["source"])["title"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    docs = documents()
    for source in SOURCES.glob("*.json"):
        if source.name in {"travel-documents.json", "manifest.json"}: continue
        obj = json.loads(source.read_text())
        if isinstance(obj, dict) and "sections" in obj:
            obj["text_sha256"] = hashlib.sha256("\n".join(s["text"] for s in obj["sections"]).encode()).hexdigest()
            source.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    spec = SOURCES / "travel-documents.json"
    spec.write_text(json.dumps({"compiled_at": COMPILED_AT, "documents": docs}, ensure_ascii=False, indent=2) + "\n")
    manifest = []
    for d in docs:
        sources = [{k: v for k, v in s.items() if k != "receipt"} for s in d["sources"]]
        times = [s.get("observed_at", s.get("retrieved_at")) for s in sources]
        manifest.append({"id": d["id"], "filename": d["filename"], "title": d["title"], "city": d["city"], "document_type": d["document_type"], "source": d["source"], "compiled_at": COMPILED_AT, "observed_at": min(times), "source_urls": [s["source_url"] for s in sources], "property_ids": d["property_ids"], "license": d["license"], "sources": sources})
    (ROOT / "data/document-sources.json").write_text(json.dumps({"documents": manifest}, ensure_ascii=False, indent=2) + "\n")
    if not args.prepare_only:
        subprocess.run([shutil.which("node") or "node", str(ROOT / "tools/data-authoring/render_travel_documents.cjs")], cwd=ROOT, check=True, env=os.environ)
    print(json.dumps({"documents": len(docs), "chapters": sum(len(d["chapters"]) for d in docs), "characters": {d["id"]: sum(len(b.get("text", "")) for c in d["chapters"] for b in c["blocks"]) for d in docs}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
