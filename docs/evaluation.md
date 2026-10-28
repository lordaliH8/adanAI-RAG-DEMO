# راهنمای ارزیابی

برای بررسی سریع، ابتدا دمو را طبق README اجرا کنید و سؤال‌های جدول نمونه‌ها را در رابط بپرسید. در «جزئیات»، نتیجه را با SQL، داده منبع، صفحه PDF و خروجی ابزار مقایسه کنید. شماره گزینه، تاریخ، واحد پول و دامنه هزینه را در ادامه گفتگو نیز بررسی کنید.

## آزمون خودکار بدون کلید

```bash
./scripts/test-docker.sh
```

این فرمان دو image آزمون می‌سازد و آزمون‌های Python و JavaScript را در container بدون شبکه اجرا می‌کند. فایل `.env` و volume برنامه به آن‌ها داده نمی‌شود. مدل‌های جایگزین در آزمون‌ها، fixtureهای صریح و محدود به آزمون‌اند؛ نتیجه آن‌ها اجرای زنده LLM نیست.

## اجرای مجموعه سناریوها

`evaluation/cases.json` سی سناریوی پذیرش دارد. معیارهای عددی و صفحات مرجع، فقط در ارزیاب استفاده می‌شوند و به API فرستاده نمی‌شوند. حالت `offline` محافظ SQL و خطاهای تزریق‌شده ارائه‌دهنده را می‌سنجد؛ `live` واقعاً API برنامه را فراخوانی می‌کند؛ `retrieval` بازیابی را مقایسه می‌کند. اجرای زنده از اعتبار ارائه‌دهنده استفاده می‌کند.

پس از اجرای Compose و ساخت image آزمون با فرمان بالا، نمونه زیر فقط به شبکه و volume همین دمو متصل می‌شود:

```bash
docker run --rm --network hamsafar-demo_default \
  --mount source=hamsafar-demo_travel_state,target=/app/var,readonly \
  --mount "type=bind,source=$PWD/reports,target=/app/reports" \
  hamsafar-demo-tests:local \
  python -m evaluation.run --mode offline \
  --output reports/evaluation-local-offline.json
```

برای چند نمونه زنده:

```bash
docker run --rm --network hamsafar-demo_default \
  --mount source=hamsafar-demo_travel_state,target=/app/var,readonly \
  --mount "type=bind,source=$PWD/reports,target=/app/reports" \
  hamsafar-demo-tests:local \
  python -m evaluation.run --base-url http://api:8000 --mode live \
  --case-ids DOC-01 SQL-01 CALC-01 \
  --output reports/evaluation-local-live.json
```

برای مقایسه بازیابی، `--mode retrieval --max-cases 2` را جایگزین کنید. گزینه `--include-rerank` یک فراخوانی مدل اضافه دارد. بدون این گزینه، Dense، BM25 و Hybrid بررسی می‌شوند. این دستورها نام پیش‌فرض پروژه Compose را فرض می‌کنند؛ اگر نام پروژه را عوض کرده‌اید، نام network و volume را نیز تغییر دهید.

## تفسیر نتایج

قبولی بررسی خودکار عدد، مسیر ابزار یا پوشش صفحه، به‌تنهایی صحت معنایی تمام پاسخ را ثابت نمی‌کند. متن پاسخ را با شواهد بررسی کنید. خطای ارائه‌دهنده، نبود داده و نبود قطعی سرویس سه نتیجه متفاوت‌اند. گزارش، تعداد اجراشده و اجرا‌نشده را جدا نشان می‌دهد؛ نمونه‌های چندمرحله‌ای طبیعی نیز در گزارش‌های مکالمه ذخیره شده‌اند.

گزارش‌های انتشار جاری در پوشه `reports` قرار دارند. گزارش‌های انتشار، اجرای زنده، آزمون HTTP با سرویس شبیه‌سازی‌شده و خطای تزریق‌شده را از هم جدا می‌کنند. سؤال‌های پذیرش از تاریخ‌های صریح استفاده می‌کنند؛ روز ثابت رابط از تنظیم `AS_OF` می‌آید.
