#!/usr/bin/env node
// Chromium provides native Arabic shaping and a searchable Unicode text layer.
// Source selection lives in build_travel_documents.py, outside the runtime image.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { chromium } = require('playwright-core');
const root = path.resolve(__dirname, '../..');
const spec = JSON.parse(fs.readFileSync(path.join(__dirname, 'sources/travel-documents.json')));
const out = path.join(root, 'output/pdf');
const review = path.join(root, 'output/review/travel');
const corpus = path.join(root, 'data/documents');
const esc = x => String(x ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const inline = x => esc(x).replace(/(?:https?:\/\/[^\s<]+|[A-Za-z][A-Za-z0-9_.\/-]*|[0-9]+(?:[-,.\/:][0-9]+)*|[۰-۹٠-٩]+(?:[-٬،,.\/:][۰-۹٠-٩]+)*)/g, t => `<bdi dir="ltr">${t}</bdi>`);
const fa = x => String(x).replace(/[0-9]/g, n => '۰۱۲۳۴۵۶۷۸۹'[n]);
const publishers = {mashhad:'ویکی‌سفر',shiraz:'ویکی‌سفر',isfahan:'ویکی‌سفر',ferdowsi:'ویکی‌پدیا',hafezieh:'ویکی‌پدیا',eram:'ویکی‌پدیا',eghamat24:'اقامت بیست و چهار',alibaba:'علی‌بابا'};
function block(b) {
 if(b.type==='paragraph') return `<p>${inline(b.text)}</p>`;
 if(b.type==='heading') return `<h3>${inline(b.text)}</h3>`;
 if(b.type==='callout') return `<aside>${inline(b.text)}</aside>`;
 if(b.type==='table') return `<table><thead><tr>${b.headers.map(v=>`<th>${inline(v)}</th>`).join('')}</tr></thead><tbody>${b.rows.map(r=>`<tr>${r.map(v=>`<td>${inline(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
 throw new Error(`Unknown block ${b.type}`);
}
function blocksFor(chapter) {
 const result=[];
 for (const b of chapter.blocks) {
  if (b.type !== 'paragraph' || b.text.length < 800) {result.push(block(b));continue;}
  // Split long source paragraphs at sentence boundaries so a single paragraph
  // does not leave half a page empty. The original word order is preserved.
  const sentences=b.text.match(/[^.!؟]+[.!؟]?/g)||[b.text];let current='';
  for(const sentence of sentences){if(current.length>500){result.push(block({type:'paragraph',text:current.trim()}));current=''}current+=sentence}
  if(current.trim())result.push(block({type:'paragraph',text:current.trim()}));
 }
 const grouped=[];
 for(let i=0;i<result.length;i++){
  if(result[i].startsWith('<h3>') && i+1<result.length){grouped.push(`<div class="keep-together">${result[i]}${result[++i]}</div>`)}
  else grouped.push(result[i]);
 }
 return grouped;
}
const css = `
@page{size:A4;margin:0}*{box-sizing:border-box}html,body{margin:0;padding:0;background:#e9eeed;color:#203b3b;font-family:Tahoma,'DejaVu Sans',sans-serif;font-size:11.4pt}
.sheet{width:210mm;height:297mm;background:white;margin:0 auto 8mm;padding:39mm 20mm 24mm;position:relative;break-after:page;overflow:hidden}.sheet:last-child{break-after:auto}
header{position:absolute;top:12mm;right:20mm;left:20mm;border-bottom:1.5px solid #38736b;padding-bottom:4mm;display:flex;justify-content:space-between;gap:10mm;font-size:8.4pt;line-height:1.9}.brand{font-size:12.5pt;color:#24594f;font-weight:bold}.serial{direction:ltr;font-family:Arial,sans-serif;font-size:8pt;color:#617674}
.content{height:230mm;line-height:1.8;display:flow-root}.content>h2{font-size:15pt;line-height:1.8;margin:0 0 4mm;color:#1e584e}.context{font-size:8pt;color:#60736d;margin:0 0 3mm}.section-code{font-family:Arial,sans-serif;font-size:10pt;color:#6d827a;direction:ltr}
p{margin:0 0 3.3mm;overflow-wrap:break-word;text-align:right}h3{font-size:11.6pt;margin:4mm 0 2mm;line-height:1.8;color:#2a5a50}bdi{direction:ltr;unicode-bidi:isolate;overflow-wrap:anywhere}aside{font-size:10.1pt;line-height:1.95;background:#eef5f0;border-right:3px solid #6d9780;padding:3mm;margin:3mm 0}
table{width:100%;border-collapse:collapse;margin:3mm 0 4mm;font-size:10pt;line-height:1.9;table-layout:fixed}th,td{padding:2.8mm;border:1px solid #d3dfd9;vertical-align:top;text-align:right}th{background:#e5efea}tbody tr:nth-child(even){background:#f7faf8}
.credits{font-size:8.4pt;line-height:1.9;color:#52645d}.credits p{margin-bottom:2mm}.credits bdi{white-space:normal;font-family:Arial,sans-serif;font-size:7.3pt;word-break:break-all}
footer{position:absolute;bottom:10mm;right:20mm;left:20mm;border-top:1px solid #dae4de;padding-top:3mm;display:flex;justify-content:space-between;font-size:8pt;color:#64786e}.page-count{white-space:nowrap}
@media print{html,body{background:white}.sheet{margin:0}}`;
async function main(){
 for(const p of [out,review,corpus])fs.mkdirSync(p,{recursive:true});
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage();
 const audit=[];
 try{
 for(const doc of spec.documents){
  const html=`<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8"><title>${esc(doc.title)}</title><style>${css}</style></head><body></body></html>`;
  await page.setContent(html);
  const result=await page.evaluate(({doc,publishers,blocks,noticeHtml})=>{
   const escape = x=>String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
   let count=0;const pages=[];
   function fresh(chapter,continuation=false){
    const sheet=document.createElement('section');sheet.className='sheet';sheet.dataset.property=chapter.property_id||'';sheet.dataset.source=chapter.source;sheet.dataset.section=chapter.code;
    sheet.innerHTML=`<header><div><div class="brand">${escape(doc.title)}</div><div>${escape(doc.city||'راهنمای عمومی رزرو')} | گردآوری: <bdi dir="ltr">2026-10-27</bdi></div></div><div class="serial">${escape(doc.id)}</div></header><div class="content"><div class="context">منبع: ${publishers[chapter.source]||'منابع ذکرشده'}${chapter.source_title?`؛ ${escape(chapter.source_title)}`:''}${chapter.property_id?` | شناسه هتل <bdi dir="ltr">${chapter.property_id}</bdi>`:''} | ثبت منبع: <bdi dir="ltr">${doc.document_type==='city_guide'?'2026-10-27':'2026-10-18'}</bdi></div><h2><bdi dir="ltr" class="section-code">${chapter.code}</bdi> | ${escape(chapter.title)}${continuation?' (ادامه)':''}</h2></div><footer><span>${doc.document_type==='city_guide'?'گزیده متن آزاد؛ CC BY-SA 4.0':'یادداشت مستند از منابع عمومی؛ شرایط وابسته به رزرو'}</span><span class="page-count"></span></footer>`;
    document.body.appendChild(sheet);count++;pages.push(sheet);return sheet.querySelector('.content');
   }
   for(let c=0;c<doc.chapters.length;c++){
    const chapter=doc.chapters[c];let content=fresh(chapter);let blockCount=0;
    if(c===0){let notice=document.createElement('aside');notice.innerHTML=noticeHtml;content.appendChild(notice)}
    for(const html of blocks[c]){
     const wrap=document.createElement('div');wrap.innerHTML=html;const element=wrap.firstElementChild;content.appendChild(element);
     if(content.scrollHeight>content.clientHeight+1){
      element.remove();if(blockCount===0)throw new Error(`Oversized block in ${doc.id}/${chapter.code}`);
      content=fresh(chapter,true);content.appendChild(element);blockCount=0;
      if(content.scrollHeight>content.clientHeight+1)throw new Error(`Single block cannot fit ${doc.id}/${chapter.code}`);
     }
     blockCount++;
    }
   }
   const credits=document.createElement('div');credits.className='credits';
   credits.innerHTML='<h3>منابع و حقوق استفاده</h3>'+(doc.document_type==='city_guide'?'<p>گزینش موضوعی، بازچینی و ویرایش نگارشی انجام شده و قیمت‌ها، زمان‌بندی‌های قدیمی و بخش‌های نامرتبط کنار گذاشته شده‌اند. متن اقتباسی با مجوز CC BY-SA 4.0 عرضه می‌شود. اعتبار نویسندگان از طریق صفحه و تاریخچه هر منبع قابل پیگیری است.</p>':'<p>این گردآوری شامل یادداشت‌های مستند و گزیده شرایط ثبت‌شده است و سند صادرشده از هتل یا فروشنده نیست. حقوق متن و نشان تجاری منابع تجاری برای صاحبان آن‌ها محفوظ است. اطلاعات ثبت‌شده به معنای تضمین شرایط امروز نیست.</p>')+doc.sources.map(s=>`<p>${escape(s.title)}<br><bdi dir="ltr">${escape(decodeURI(s.revision_url||s.source_url))}</bdi>${s.revision_url?`<br><bdi dir="ltr">${escape(decodeURI(s.source_url))}?action=history</bdi>`:''}</p>`).join('')+(doc.document_type==='city_guide'?'<p><bdi dir="ltr">https://creativecommons.org/licenses/by-sa/4.0/</bdi></p>':'');
   let content=pages.at(-1).querySelector('.content');content.appendChild(credits);
   if(content.scrollHeight>content.clientHeight+1){credits.remove();credits.querySelector('h3').remove();content=fresh({code:doc.chapters.at(-1).code,title:'منابع و حقوق استفاده',source:doc.chapters.at(-1).source});content.appendChild(credits)}
   pages.forEach((sheet,i)=>{sheet.querySelector('.page-count').textContent=`صفحه ${i+1} از ${pages.length}`});
   return pages.map((sheet,i)=>({page:i+1,section:sheet.dataset.section,property_id:sheet.dataset.property||null,source:sheet.dataset.source,characters:sheet.querySelector('.content').innerText.length,overflow:sheet.querySelector('.content').scrollHeight>sheet.querySelector('.content').clientHeight+1}));
  },{doc,publishers,blocks:doc.chapters.map(blocksFor),noticeHtml:inline(doc.notice)});
  if(result.some(r=>r.overflow))throw new Error('Page overflow '+doc.id);
  await page.evaluate(()=>document.fonts.ready);
  fs.writeFileSync(path.join(review,doc.filename.replace('.pdf','.html')),await page.content());
  await page.pdf({path:path.join(out,doc.filename),format:'A4',printBackground:true,preferCSSPageSize:true,tagged:true});
  fs.copyFileSync(path.join(out,doc.filename),path.join(corpus,doc.filename));
  const bytes=fs.readFileSync(path.join(out,doc.filename));
  audit.push({document_id:doc.id,filename:doc.filename,sha256:crypto.createHash('sha256').update(bytes).digest('hex'),pages:result.length,layout:result});
  process.stdout.write(JSON.stringify({document_id:doc.id,pages:result.length,overflow:false})+'\n');
 }
 }finally{await browser.close()}
 fs.writeFileSync(path.join(__dirname,'sources/manifest.json'),JSON.stringify({compiled_at:spec.compiled_at,documents:audit},null,2)+'\n');
}
main().catch(e=>{process.stderr.write(e.stack+'\n');process.exit(1)});
