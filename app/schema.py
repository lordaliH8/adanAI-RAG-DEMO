"""Travel snapshot schema and model-facing semantics; no evaluation answers."""

MIGRATION_VERSION = 2
TABLE_COLUMNS = {
    "sources": {"id": "TEXT", "homepage": "TEXT"},
    "records": {"id": "TEXT", "source": "TEXT", "source_id": "TEXT", "entity_type": "TEXT", "source_url": "TEXT", "name_fa": "TEXT", "city_fa": "TEXT", "observed_at": "TEXT", "data_json": "TEXT", "evidence_json": "TEXT"},
    "properties": {"record_id": "TEXT", "city_fa": "TEXT", "property_type": "TEXT", "stars": "REAL", "rating": "REAL", "rating_scale": "REAL", "review_count": "INTEGER", "headline_price_irr": "INTEGER", "price_kind": "TEXT", "checkin_time": "TEXT", "checkout_time": "TEXT", "address_fa": "TEXT", "amenities_json": "TEXT"},
    "transport_offers": {"record_id": "TEXT", "mode": "TEXT", "origin_fa": "TEXT", "destination_fa": "TEXT", "service_date": "TEXT", "departure_at": "TEXT", "arrival_at": "TEXT", "amount_irr": "INTEGER", "availability": "TEXT", "available_seats": "INTEGER", "operator_name": "TEXT", "offer_kind": "TEXT", "price_basis": "TEXT", "search_completed": "INTEGER", "child_price_irr": "INTEGER", "infant_price_irr": "INTEGER", "baggage_kg": "INTEGER", "flight_number": "TEXT"},
    "stay_rates": {"record_id": "TEXT", "property_id": "TEXT", "room_id": "TEXT", "room_name": "TEXT", "rate_plan_id": "TEXT", "rate_plan_name": "TEXT", "service_date": "TEXT", "checkin": "TEXT", "checkout": "TEXT", "amount_irr": "INTEGER", "availability": "TEXT", "occupancy": "INTEGER", "inventory": "INTEGER", "min_stay": "INTEGER", "max_stay": "INTEGER", "closed": "INTEGER", "close_to_arrival": "INTEGER", "close_to_departure": "INTEGER", "board_type": "TEXT", "price_basis": "TEXT", "extra_capacity": "INTEGER", "cancelable": "INTEGER"},
    "coverage": {"id": "TEXT", "source": "TEXT", "mode": "TEXT", "origin_fa": "TEXT", "destination_fa": "TEXT", "service_date": "TEXT", "status": "TEXT", "checked_at": "TEXT", "result_count": "INTEGER", "reason": "TEXT", "query_url": "TEXT"},
}
SCHEMA_SQL = """
CREATE TABLE sources(id TEXT PRIMARY KEY, homepage TEXT NOT NULL);
CREATE TABLE records(
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id), source_id TEXT NOT NULL,
 entity_type TEXT NOT NULL CHECK(entity_type IN ('property','route','offer','content')),
 source_url TEXT NOT NULL, name_fa TEXT NOT NULL, city_fa TEXT, observed_at TEXT NOT NULL,
 data_json TEXT NOT NULL CHECK(json_valid(data_json)), evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
 UNIQUE(source,entity_type,source_id)
);
CREATE TABLE properties(
 record_id TEXT PRIMARY KEY REFERENCES records(id), city_fa TEXT, property_type TEXT,
 stars REAL CHECK(stars IS NULL OR stars BETWEEN 0 AND 5), rating REAL,
 rating_scale REAL CHECK(rating_scale IS NULL OR rating_scale>0),
 review_count INTEGER CHECK(review_count IS NULL OR review_count>=0),
 headline_price_irr INTEGER CHECK(headline_price_irr IS NULL OR headline_price_irr>=0),
 price_kind TEXT NOT NULL CHECK(price_kind='undated_headline'), checkin_time TEXT, checkout_time TEXT,
 address_fa TEXT, amenities_json TEXT NOT NULL CHECK(json_valid(amenities_json))
);
CREATE TABLE transport_offers(
 record_id TEXT PRIMARY KEY REFERENCES records(id), mode TEXT NOT NULL CHECK(mode IN ('flight','bus','train')),
 origin_fa TEXT NOT NULL, destination_fa TEXT NOT NULL, service_date TEXT NOT NULL,
 departure_at TEXT, arrival_at TEXT, amount_irr INTEGER NOT NULL CHECK(amount_irr>0),
 availability TEXT NOT NULL CHECK(availability IN ('available','sold_out','unavailable','unknown','calendar_indicated')),
 available_seats INTEGER CHECK(available_seats IS NULL OR available_seats>=0), operator_name TEXT,
 offer_kind TEXT NOT NULL CHECK(offer_kind IN ('departure','daily_fare_aggregate')), price_basis TEXT NOT NULL,
 search_completed INTEGER CHECK(search_completed IS NULL OR search_completed IN (0,1)),
 child_price_irr INTEGER CHECK(child_price_irr IS NULL OR child_price_irr>=0),
 infant_price_irr INTEGER CHECK(infant_price_irr IS NULL OR infant_price_irr>=0),
 baggage_kg INTEGER CHECK(baggage_kg IS NULL OR baggage_kg>=0), flight_number TEXT,
 CHECK((offer_kind='departure' AND departure_at IS NOT NULL AND price_basis='per_adult_one_way')
    OR (offer_kind='daily_fare_aggregate' AND departure_at IS NULL AND arrival_at IS NULL
        AND price_basis='per_adult_one_way_from'))
);
CREATE TABLE stay_rates(
 record_id TEXT PRIMARY KEY REFERENCES records(id), property_id TEXT NOT NULL REFERENCES properties(record_id),
 room_id TEXT NOT NULL, room_name TEXT, rate_plan_id TEXT NOT NULL, rate_plan_name TEXT,
 service_date TEXT NOT NULL, checkin TEXT NOT NULL, checkout TEXT NOT NULL,
 amount_irr INTEGER NOT NULL CHECK(amount_irr>0),
 availability TEXT NOT NULL CHECK(availability IN ('available','unavailable','unknown')),
 occupancy INTEGER CHECK(occupancy IS NULL OR occupancy>0), inventory INTEGER CHECK(inventory IS NULL OR inventory>=0),
 min_stay INTEGER CHECK(min_stay IS NULL OR min_stay>=0), max_stay INTEGER CHECK(max_stay IS NULL OR max_stay>=0),
 closed INTEGER CHECK(closed IS NULL OR closed IN (0,1)),
 close_to_arrival INTEGER CHECK(close_to_arrival IS NULL OR close_to_arrival IN (0,1)),
 close_to_departure INTEGER CHECK(close_to_departure IS NULL OR close_to_departure IN (0,1)),
 board_type TEXT, price_basis TEXT NOT NULL CHECK(price_basis='per_room_per_night'),
 extra_capacity INTEGER CHECK(extra_capacity IS NULL OR extra_capacity>=0),
 cancelable INTEGER CHECK(cancelable IS NULL OR cancelable IN (0,1)),
 CHECK(service_date=checkin AND date(checkout)=date(checkin,'+1 day')),
 UNIQUE(property_id,room_id,rate_plan_id,service_date)
);
CREATE TABLE coverage(
 id TEXT PRIMARY KEY, source TEXT NOT NULL REFERENCES sources(id), mode TEXT NOT NULL,
 origin_fa TEXT NOT NULL, destination_fa TEXT NOT NULL, service_date TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('planned','observed','empty','blocked','not_exposed','failed')),
 checked_at TEXT, result_count INTEGER CHECK(result_count IS NULL OR result_count>=0), reason TEXT, query_url TEXT,
 UNIQUE(source,mode,origin_fa,destination_fa,service_date)
);
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX ix_record_source ON records(source,entity_type,source_id);
CREATE INDEX ix_property_city ON properties(city_fa);
CREATE INDEX ix_transport_search ON transport_offers(origin_fa,destination_fa,service_date,mode,availability);
CREATE INDEX ix_stay_search ON stay_rates(property_id,service_date,availability,occupancy);
CREATE INDEX ix_coverage_search ON coverage(source,mode,origin_fa,destination_fa,service_date);
"""

SCHEMA_CONTEXT = """پایگاه داده سفر «همسفر»، گردآوری واقعی از صفحات عمومی؛ روز مرجع از تنظیمات گفتگو می‌آید.
شناسه منبع دقیق: eghamat24=اقامت۲۴/اقامت بیست‌وچهار، alibaba=علی‌بابا، safarmarket=سفرمارکت.
records.entity_type فقط چهار مقدار واقعی property، offer، route و content دارد. تمام transport_offers و stay_rates رکورد offer هستند؛ 'transport'، 'flight'، 'stay' یا 'hotel' نوع موجودیت رکورد نیستند.
records.source و sources.id همین نام‌های انگلیسی‌اند؛ '24' یا نام فارسی مقدار ستون source نیست.
کلید اصلی records فقط id است. ستون record_id متعلق به properties/transport_offers/stay_rates است؛ در زیرپرس‌وجوی records از r.id با alias صریح استفاده کن.
زمان ثبت قیمت و ظرفیت از records.observed_at هر رکورد خوانده می‌شود؛ این داده استعلام زنده یا تضمین رزرو نیست.
فقط SELECT خواندنی روی جدول‌های زیر مجاز است. نام ستون‌ها انگلیسی و شهر/شرح فارسی‌اند:
""" + "\n".join(name + "(" + ", ".join(columns) + ")" for name, columns in TABLE_COLUMNS.items()) + """

روابط: records.source→sources.id؛ properties.record_id→records.id؛
transport_offers.record_id→records.id؛ stay_rates.record_id→records.id و
stay_rates.property_id→properties.record_id؛ coverage.source→sources.id.
برای نام هتل یا URL و زمان مشاهده به records متصل شو؛ property_id یک شناسه داخلی
هش‌شده است، نه شماره عددی هتل در سایت. records.source_id شناسه اصلی همان منبع است.
availability در transport_offers و stay_rates یک وضعیت متنی TEXT است، نه boolean یا عدد1/0. مقادیر حمل‌ونقل available/sold_out/unavailable/unknown/calendar_indicated و مقادیر اقامت available/unavailable/unknown هستند؛ برای موجودی قابل مشاهده availability='available' بنویس، نه availability=1.
معنای داده‌های پرواز/قطار/اتوبوس:
- فقط offer_kind='departure' حرکت مشخص دارد. daily_fare_aggregate فقط کف قیمت
  تقویمی روزانه است؛ هیچ ساعت، شماره سرویس یا ظرفیت قطعی از آن استنباط نکن.
- قیمت amount_irr ریال صحیح برای یک بزرگسال و یک‌طرفه است. همه محاسبات ابتدا
  به ریال انجام شوند؛ تبدیل به تومان با تقسیم بر 10 در ابزار محاسبه. بعضی مبالغ
  بر 10 بخش‌پذیر نیستند؛ از integer division یا گرد کردن زودهنگام استفاده نکن.
- برای گزینه مناسب، available و available_seats>=تعداد مسافر لازم است؛ تقویم
  unknown/calendar_indicated یا sold_out را صندلی قابل رزرو معرفی نکن.
- departure_at/arrival_at زمان محلی با offset هستند؛ service_date روز میلادی ISO است.
  ورود اتوبوس NULL است؛ از ساعت حرکت، زمان ورود یا هماهنگی هتل را حدس نزن.
- child_price_irr/infant_price_irr/baggage_kg در صورت NULL نامعلوم‌اند؛ قیمت کودک
  را از نرخ بزرگسال حدس نزن. جزئیات قانون استرداد در records.data_json حفظ شده‌اند.
معنای اقامت:
- amount_irr قیمت یک اتاق برای یک شب، نه هر نفر یا کل سفر است. برای دو نفر در
  یک اتاق دوتخته، نرخ اتاق را دوباره در دو ضرب نکن.
- تمام شب‌های [checkin,checkout) لازم‌اند. برای یک اقامت پیوسته باید property_id،
  room_id،rate_plan_id یکسان بمانند؛ COUNT(DISTINCT service_date)=تعداد شب‌ها.
  هر روز دقیقاً یک ردیف دارد. جمع ردیف هتل‌ها/اتاق‌ها/پلن‌های متفاوت قیمت سفر نیست.
- occupancy ظرفیت پایه است؛ extra_capacity تخت اضافه با نرخ/شرایط جداست و رایگان
  یا قابل استفاده فرض نمی‌شود. inventory ظرفیت اتاق است؛ با تعداد مسافر مقایسه نکن.
- همه شب‌ها باید available، inventory کافی و closed=0 داشته باشند؛ محدودیت
  min_stay/max_stay و close_to_arrival/close_to_departure را بررسی کن.
  NULL در محدودیت‌ها یعنی ثبت نشده، نه اثبات بدون محدودیت بودن.
- board_type='bed_and_breakfast' با room_only فرق دارد؛ داشتن رستوران در معرفی
  هتل به معنی صبحانه رایگان در هر پلن نیست. cancelable به معنی لغو رایگان نیست.
- properties.headline_price_irr فقط قیمت از/تبلیغاتی بدون تاریخ و ظرفیت مشخص است؛
  آن را جانشین stay_rates یا قیمت اقامت در تاریخ خواسته‌شده نکن.
coverage گزارش گردآوری است، نه موجودی: planned/failed/not_exposed یعنی شاهد کافی
نداریم؛ empty هم لزوماً نبود سرویس نیست. observed ممکن است فقط ردیف فروخته‌شده
یا کف قیمت تقویم دیده باشد؛ availability باید جدا بررسی شود. نبود ردیف برگشت/قطار
یعنی داده نداریم، نه اینکه در واقع هیچ سرویسی وجود ندارد. NULL را صفر نکن.
ابهام شهر، تاریخ، رفت/برگشت، تعداد بزرگسال/کودک، تعداد اتاق یا بودجه باید روشن شود.
هزینه‌های بدون داده مثل غذا، ترانسفر و برگشت به جمع اضافه نشوند؛ دامنه جمع را بگو.
SQL مبلغ/تعداد واقعی را برگرداند؛ جمع نهایی بودجه، اختلاف و تبدیل واحد با calculator.
این دستیار مقایسه و برآورد از snapshot می‌دهد و خرید، رزرو یا پرداخت انجام نمی‌دهد.
"""

SCHEMA_CONTEXT += "\nنام‌ها و شناسه‌های ذخیره‌شده ارقام ASCII دارند؛ نام کوتاه هتل را برابر دقیق با نام کامل+شهر نگذار. شناسه عددی سایت، records.source_id است، نه stay_rates.property_id. برای شمارش رکوردهای اقامتگاه، records با entity_type=property یا اتصال properties به records معادل‌اند؛ تعداد نام یکتا تضمین تعداد هتل فیزیکی نیست.\n"
