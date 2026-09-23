# Purchase dashboard generation + IBM Cloud Object Storage upload.
# Uses only rows already returned by Purchase; no SQL or LLM calls.
from __future__ import annotations
import html,json,os,re,uuid
from dotenv import load_dotenv
from datetime import datetime,timezone
from typing import Any,Dict,List,Optional,Tuple
from urllib.parse import urlparse
import ibm_boto3
from ibm_botocore.client import Config

load_dotenv()

GRAPH_ENABLED=os.getenv('GRAPH_ENABLED','true').lower()=='true'
COS_API_KEY_ID=os.getenv('COS_API_KEY_ID','').strip()
COS_RESOURCE_CRN=os.getenv('COS_RESOURCE_CRN','').strip()
COS_ENDPOINT=os.getenv('COS_ENDPOINT','').strip()
COS_BUCKET=os.getenv('COS_BUCKET','').strip()
COS_PUBLIC_URL=os.getenv('COS_PUBLIC_URL','').strip()
# IMPORTANT: direct public URL is disabled unless explicitly enabled.
COS_USE_PUBLIC_URL=os.getenv('COS_USE_PUBLIC_URL','false').lower()=='true'
COS_REQUIRED=os.getenv('COS_REQUIRED','false').lower()=='true'
COS_OBJECT_PREFIX=os.getenv('COS_OBJECT_PREFIX','purchase-dashboards').strip('/')
COS_URL_EXPIRATION_SECONDS=int(os.getenv('COS_URL_EXPIRATION_SECONDS','604800'))
COS_HMAC_ACCESS_KEY_ID=os.getenv('COS_HMAC_ACCESS_KEY_ID','').strip()
COS_HMAC_SECRET_ACCESS_KEY=os.getenv('COS_HMAC_SECRET_ACCESS_KEY','').strip()

TECHNICAL_FIELDS={'line_record_count','row_count','record_count','response_length','api_response_length','status_code','http_status','technical_metadata','metadata','error','success','message'}
PURCHASE_ID_FIELDS={'id','uuid','record_id','row_id','po_number','po_item','pr_number','pr_item','po_document','purchase_order','purchase_order_number','purchase_requisition','purchase_requisition_number','material_document','material_document_number','material_doc','document_number','document_no','document_id','vendor_number','supplier_number','plant_code','company_code','item_number','item_no','release_level'}
DIMENSION_ALIASES={
 'vendor':('Name_of_Supplier','Supplier','Vendor','Vendor_Name','Supplier_Name','Supplier_Name1'),
 'supplier':('Name_of_Supplier','Supplier','Vendor','Vendor_Name','Supplier_Name','Supplier_Name1'),
 'plant':('PO_Plant','Plant','Plant_Code','Plant_Name'),
 'department':('Department','Department_Name','Dept','Dept_Name','PO_Department_Name'),
 'company':('Company_Code','Company','Company_Name'),
 'material':('Material','Material_Code','Material_Description','Material_Desc'),
 'status':('Status','PO_Status','PR_Status','Release_Status'),
 'requisitioner':('Requisitioner','Requisitioner_Name','PR_Created_By','Created_By')}
DIMENSION_LABELS={'vendor':'Vendor','supplier':'Supplier','plant':'Plant','department':'Department','company':'Company','material':'Material','status':'Status','requisitioner':'Requisitioner'}
METRIC_LABELS={'distinct_count':'PO Count','count':'Count','record_count':'Count','po_count':'PO Count','pr_count':'PR Count','total':'Total','total_value':'Total Value','pr_value':'PR Value','value':'Value','amount':'Amount','quantity':'Quantity'}
GENERIC_DIMENSION_ALIASES=('group_value_1','group_value','group_value_2','group_value_3','dimension','dimension_value','category')

def _normalise(v:Any)->str:return re.sub(r'[^a-z0-9]+','_',str(v).strip().lower()).strip('_')
def _display_name(v:Any)->str:
 t='' if v is None else str(v).strip(); return t or 'Unknown'
def _safe_html(v:Any)->str:return html.escape(str(v),quote=True)
def _json_for_html(v:Any)->str:return json.dumps(v,ensure_ascii=False,default=str).replace('</','<\\/')
def _to_number(v:Any)->Optional[float]:
 if v is None or isinstance(v,bool):return None
 if isinstance(v,(int,float)):return float(v)
 t=str(v).strip()
 if not t:return None
 t=t.replace(',','').replace('₹','').replace('$','').replace('€','').replace('£','')
 if t.endswith('-'):t='-'+t[:-1]
 if t.startswith('(') and t.endswith(')'):t='-'+t[1:-1]
 try:return float(t)
 except (ValueError,TypeError):return None
def _is_technical_field(c:str)->bool:
 n=_normalise(c);return n in TECHNICAL_FIELDS or n.endswith('_metadata') or n.startswith('metadata_')
def _is_obvious_id(c:str)->bool:
 n=_normalise(c);return n in PURCHASE_ID_FIELDS or n.endswith('_uuid') or n.endswith('_id') or n.endswith('_record_id') or n.endswith('_row_id')
def _find_column(columns:List[str],candidates:Tuple[str,...])->Optional[str]:
 m={_normalise(c):c for c in columns}
 for c in candidates:
  if _normalise(c) in m:return m[_normalise(c)]
 return None

def _graph_required(question:str,intent:Any)->bool:
 q=question.lower().strip()
 words=('graph','chart','dashboard','visualize','visualise','visualization','visualisation','plot','vendor-wise','vendor wise','supplier-wise','supplier wise','department-wise','department wise','plant-wise','plant wise','company-wise','company wise','material-wise','material wise','requisitioner-wise','requisitioner wise','status-wise','status wise','category-wise','category wise','breakdown','distribution','ranking','rank','top ','bottom ','compare','comparison','trend','month-wise','month wise','monthly','quarter-wise','quarter wise','quarterly','year-wise','year wise','yearly','performance','over time','growth','movement')
 if any(x in q for x in words):return True
 return bool(getattr(intent,'group_by_column',None) or getattr(intent,'time_grain',None) or getattr(intent,'operation',None) in {'group_by_count','trend'})

def _business_dimension_label(group_by:Any,fallback:str)->str:
 k=_normalise(group_by) if group_by else _normalise(fallback)
 direct={'vendor':'Vendor','vendor_name':'Vendor','supplier':'Supplier','supplier_name':'Supplier','plant':'Plant','plant_name':'Plant','department':'Department','department_name':'Department','company':'Company','company_name':'Company','material':'Material','material_desc':'Material','status':'Status','status_name':'Status','requisitioner':'Requisitioner','requisitioner_name':'Requisitioner'}
 if k in direct:return direct[k]
 for ak,cands in DIMENSION_ALIASES.items():
  if k in {_normalise(x) for x in cands}:return DIMENSION_LABELS[ak]
 return fallback.replace('_',' ').title()

def _business_metric_label(metric:str,intent:Any,question:str="")->str:
 n=_normalise(metric); op=getattr(intent,'operation',None); distinct_key=_normalise(getattr(intent,'distinct_key',None) or '')
 if n in {'record_count','count','distinct_count','count_distinct','po_count','pr_count'}:
  if 'pr' in distinct_key or 'purchase_requisition' in distinct_key:return 'PR Count'
  if 'po' in distinct_key or 'purchase_order' in distinct_key:return 'PO Count'
  if n=='po_count':return 'PO Count'
  if n=='pr_count':return 'PR Count'
  q=_normalise(question)
  if 'po' in q and 'count' in q:return 'PO Count'
  if 'pr' in q and 'count' in q:return 'PR Count'
  if op=='count_distinct':return 'Distinct Count'
  return 'Count'
 if n in METRIC_LABELS:return METRIC_LABELS[n]
 return metric.replace('_',' ').title()

def _dimension(columns:List[str],rows:List[Dict[str,Any]],question:str,intent:Any)->Optional[str]:
 g=getattr(intent,'group_by_column',None)
 if g:
  x=_find_column(columns,(str(g),))
  if x:return x
  x=_find_column(columns,DIMENSION_ALIASES.get(_normalise(g),()))
  if x:return x
  # Critical mapping: semantic intent dimension -> generic SQL alias.
  x=_find_column(columns,GENERIC_DIMENSION_ALIASES)
  if x:return x
 q=question.lower()
 for word,key in (('vendor','vendor'),('supplier','supplier'),('plant','plant'),('department','department'),('company','company'),('material','material'),('requisitioner','requisitioner'),('status','status')):
  if word in q:
   x=_find_column(columns,DIMENSION_ALIASES[key]) or _find_column(columns,GENERIC_DIMENSION_ALIASES)
   if x:return x
 if getattr(intent,'time_grain',None):
  x=_find_column(columns,('Month','Month_Name','Period','Quarter','Year','Date','time','time_period','period','date','month','quarter','year'))
  if x:return x
 return None

def _metrics(columns:List[str],rows:List[Dict[str,Any]],dimension_column:Optional[str]=None)->List[str]:
 # First find every column (other than the dimension) that actually holds numeric
 # values across the rows. If there is exactly ONE such column, there is no
 # ambiguity about what the metric is -- it IS the metric, regardless of its name.
 # This is what makes multi-factor group-bys (2+ dimension columns like
 # department+status, or vendor+month) chartable: no matter how many extra
 # categorical columns are present, as long as only one column is numeric, that
 # column is the metric. The technical/id-field name exclusion only matters when
 # there's real ambiguity, i.e. more than one numeric-looking column.
 numeric_candidates=[]
 for c in columns:
  if c==dimension_column:continue
  vals=[_to_number(r.get(c)) for r in rows]; vals=[v for v in vals if v is not None]
  if not vals:continue
  numeric_candidates.append(c)
 if len(numeric_candidates)==1:return numeric_candidates
 out=[]
 for c in numeric_candidates:
  if _is_technical_field(c) or _is_obvious_id(c):continue
  n=_normalise(c)
  if n.endswith(('_number','_no','_code','_key')):continue
  out.append(c)
 return out

def _pick_metric(metrics:List[str],question:str,intent:Any)->Optional[str]:
 if not metrics:return None
 a=getattr(intent,'aggregate_column',None)
 if a:
  x=_find_column(metrics,(str(a),))
  if x:return x
 op=getattr(intent,'operation',None)
 if op=='count_distinct':
  x=_find_column(metrics,('distinct_count','po_count','count','count_distinct'))
  if x:return x
 if op in {'count','group_by_count'}:
  x=_find_column(metrics,('count','distinct_count','po_count','pr_count'))
  if x:return x
 q=question.lower()
 for m in metrics:
  n=_normalise(m)
  if any(_normalise(t) in n for t in ('value','amount','cost','price','quantity','count','total','spend','delay','days','percentage','percent','share')):return m
 if op in {'count','count_distinct','group_by_count'}:
  for m in metrics:
   if 'count' in _normalise(m):return m
 if len(metrics)==1:return metrics[0]
 for t in ('value','amount','total','cost','quantity','count'):
  for m in metrics:
   if _normalise(t) in _normalise(m):return m
 return metrics[0]

def _period_text(date_range:Any,date_label:Optional[str])->str:
 if date_label:return str(date_label)
 if isinstance(date_range,dict):
  if date_range.get('label'):return str(date_range['label'])
  if date_range.get('start') and date_range.get('end'):return f"{date_range['start']} to {date_range['end']}"
 if date_range is not None:
  if getattr(date_range,'label',None):return str(date_range.label)
  if getattr(date_range,'start',None) and getattr(date_range,'end',None):return f"{date_range.start} to {date_range.end}"
 return 'Selected reporting period'
def _chart_type(question:str,intent:Any)->str:
 if getattr(intent,'time_grain',None):return 'line'
 return 'line' if any(x in question.lower() for x in ('month-wise','month wise','monthly','quarter-wise','quarter wise','quarterly','year-wise','year wise','yearly','trend','over time','growth','performance','movement')) else 'bar'
def _chart_rows(rows:List[Dict[str,Any]],dimension:'str|List[str]',metric:str)->List[Dict[str,Any]]:
 dims=[dimension] if isinstance(dimension,str) else list(dimension)
 out=[]
 for r in rows:
  if any(r.get(d) is None for d in dims):continue
  y=_to_number(r.get(metric))
  if y is None:continue
  category=' - '.join(_display_name(r.get(d)) for d in dims)
  out.append({'category':category,'value':y})
 return out
def _extra_dimension_columns(columns:List[str],dimension_column:Optional[str],metrics:List[str])->List[str]:
 # Columns beyond the primary dimension that are neither a candidate metric nor an
 # obvious technical/id field are additional grouping factors (e.g. a second
 # group-by like status alongside department). Folding them into the chart's
 # category axis, instead of silently dropping them, is what makes 2+ factor
 # breakdowns actually show all the involved factors on the graph.
 excluded={dimension_column,*metrics}
 out=[]
 for c in columns:
  if c in excluded:continue
  if _is_technical_field(c) or _is_obvious_id(c):continue
  out.append(c)
 return out
def _key_insight(rows:List[Dict[str,Any]],dimension:str,metric:str)->str:
 if not rows:return 'No chartable business values were returned for this request.'
 hi=max(rows,key=lambda x:x['value']);lo=min(rows,key=lambda x:x['value']);total=sum(x['value'] for x in rows)
 return f"{hi['category']} has the highest {metric} at {hi['value']:,.2f}. The lowest is {lo['category']} at {lo['value']:,.2f}. Total across the displayed {dimension} breakdown is {total:,.2f}."

def _create_cos_client():
 if not COS_ENDPOINT:raise RuntimeError('COS_ENDPOINT is not configured.')
 if not COS_BUCKET:raise RuntimeError('COS_BUCKET is not configured.')
 if COS_HMAC_ACCESS_KEY_ID and COS_HMAC_SECRET_ACCESS_KEY:
  return ibm_boto3.client('s3',aws_access_key_id=COS_HMAC_ACCESS_KEY_ID,aws_secret_access_key=COS_HMAC_SECRET_ACCESS_KEY,endpoint_url=COS_ENDPOINT,config=Config(signature_version='s3v4'))
 if not COS_API_KEY_ID:raise RuntimeError('COS_API_KEY_ID is not configured.')
 kw={'ibm_api_key_id':COS_API_KEY_ID,'endpoint_url':COS_ENDPOINT,'config':Config(signature_version='oauth')}
 if COS_RESOURCE_CRN:kw['ibm_service_instance_id']=COS_RESOURCE_CRN
 return ibm_boto3.client('s3',**kw)

def _normalise_public_cos_url(base_url: str) -> str:
    """Return a safe bucket/object base URL from COS_PUBLIC_URL.

    COS_PUBLIC_URL may be either:
    - a bucket base URL, e.g. https://host/mbcnvoice
    - an endpoint URL, e.g. https://host

    It must never be treated as a placeholder/example hostname.
    """
    base_url = (base_url or '').strip().rstrip('/')
    if not base_url:
        raise RuntimeError('COS_PUBLIC_URL is empty.')

    parsed = urlparse(base_url)
    host = (parsed.hostname or '').lower()

    if parsed.scheme not in {'http', 'https'} or not host:
        raise RuntimeError('COS_PUBLIC_URL must be a valid http(s) URL.')

    # Never generate a browser link to an example/placeholder hostname.
    if host.endswith('example.com') or host.endswith('example.org') or host.endswith('example.net'):
        raise RuntimeError(
            'COS_PUBLIC_URL points to a placeholder/example hostname. '
            'Leave COS_USE_PUBLIC_URL=false unless you have a real public COS URL.'
        )

    path = parsed.path.rstrip('/')
    bucket = COS_BUCKET.strip('/')

    # If the configured URL already ends with the bucket, keep it.
    if bucket and not path.endswith('/' + bucket) and path != bucket:
        path = f'{path}/{bucket}' if path else f'/{bucket}'

    return f'{parsed.scheme}://{parsed.netloc}{path}'


def _upload_html(graph_id:str,html_content:str)->str:
    client=_create_cos_client()
    object_key=f'{COS_OBJECT_PREFIX}/{graph_id}.html'

    # The bucket is always COS_BUCKET; the prefix is part of the object key.
    client.put_object(
        Bucket=COS_BUCKET,
        Key=object_key,
        Body=html_content.encode('utf-8'),
        ContentType='text/html; charset=utf-8',
        CacheControl='no-cache',
    )

    # Direct public URLs are opt-in. When enabled, COS_PUBLIC_URL can be
    # either the COS endpoint or a bucket base URL. The bucket is added when
    # it is not already present, preventing the previous NoSuchBucket URL.
    if COS_USE_PUBLIC_URL:
        public_base = _normalise_public_cos_url(COS_PUBLIC_URL)
        return f'{public_base}/{object_key}'

    # Default: generate a temporary signed URL for the exact bucket/object.
    # This does not depend on the bucket being publicly readable.
    return client.generate_presigned_url(
        'get_object',
        Params={
            'Bucket': COS_BUCKET,
            'Key': object_key,
        },
        ExpiresIn=COS_URL_EXPIRATION_SECONDS,
    )


def _build_html(*,title:str,period:str,dimension:str,metrics:List[str],selected_metric:str,chart_type:str,metric_data:Dict[str,List[Dict[str,Any]]])->str:
 th=_safe_html(title);ph=_safe_html(period);dh=_safe_html(dimension);sj=_json_for_html(metrics);dj=_json_for_html(metric_data);smh=_safe_html(selected_metric)
 initial=metric_data.get(selected_metric,[]);total=sum(x['value'] for x in initial);hi=max(initial,key=lambda x:x['value']) if initial else None;lo=min(initial,key=lambda x:x['value']) if initial else None;avg=(total/len(initial)) if initial else 0
 hih=_safe_html(hi['category']) if hi else '—';loh=_safe_html(lo['category']) if lo else '—';hiv=f"{hi['value']:,.2f}" if hi else '—';lov=f"{lo['value']:,.2f}" if lo else '—';avgv=f"{avg:,.2f}"
 ins=_safe_html(_key_insight(initial,dimension,selected_metric))
 headline=_safe_html(f"{hi['category']} recorded the highest {selected_metric} at {hi['value']:,.2f}, while {lo['category']} was the lowest at {lo['value']:,.2f}.") if hi and lo else 'No chartable business values were returned for this request.'
 gen=datetime.now(timezone.utc).strftime('%d %b %Y, %H:%M UTC')
 if len(metrics)>1:
  selector='<div class="metric-selector"><label>Metric</label><select id="metricSelect" onchange="changeMetric(this.value)">'+''.join(f'<option value="{_safe_html(m)}" {"selected" if m==selected_metric else ""}>{_safe_html(m)}</option>' for m in metrics)+'</select></div>'
 else:selector=f'<div class="metric-display"><div class="metric-display-label">Metric</div><div class="metric-display-value">{smh}</div></div>'
 return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{th}</title><script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script><style>
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f7fb;color:#172033}}
.page{{max-width:1540px;margin:0 auto;padding:16px 34px 22px}}
.header{{background:linear-gradient(112deg,#0b2f73 0%,#1459bd 55%,#2c76dc 100%);color:white;border-radius:19px;padding:25px 30px;box-shadow:0 14px 35px rgba(19,71,145,.2);display:flex;justify-content:space-between;align-items:center;gap:20px;flex-wrap:wrap}}
.header-left{{display:flex;align-items:center;gap:16px;min-width:0}}
.header-icon{{flex:0 0 auto;width:48px;height:48px;border-radius:14px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28);display:flex;align-items:center;justify-content:center}}
.header-icon svg{{width:24px;height:24px}}
.eyebrow{{font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;opacity:.82;margin-bottom:4px}}
h1{{margin:0;font-size:27px;line-height:1.25;font-weight:750;overflow-wrap:anywhere}}
.header-right{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.period-badge{{min-width:170px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);padding:11px 16px;border-radius:14px;backdrop-filter:blur(8px);display:flex;gap:10px;align-items:center}}
.period-badge svg{{width:18px;height:18px;flex:0 0 auto;opacity:.9}}
.period-badge-label{{font-size:11px;font-weight:700;text-transform:uppercase;opacity:.8;margin-bottom:3px}}
.period-badge-value{{font-size:13px;font-weight:650}}
.metric-selector,.metric-display,.topn-selector{{min-width:170px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28);padding:11px 16px;border-radius:14px;backdrop-filter:blur(8px)}}
.metric-selector label,.metric-display-label,.topn-selector label{{display:block;font-size:11px;font-weight:700;text-transform:uppercase;opacity:.8;margin-bottom:6px}}
.metric-selector select,.topn-selector select{{width:100%;border:0;outline:0;padding:8px 10px;border-radius:9px;font-size:14px;font-weight:650;background:white;color:#12305e}}
.metric-display-value{{font-size:14px;font-weight:700}}
.cards{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px;margin-top:19px}}
.card{{background:white;border:1px solid #e4e9f1;border-radius:16px;padding:19px 21px;box-shadow:0 8px 25px rgba(24,45,80,.06);display:flex;align-items:center;gap:15px}}
.card-icon{{flex:0 0 auto;width:44px;height:44px;border-radius:13px;display:flex;align-items:center;justify-content:center}}
.card-icon svg{{width:21px;height:21px}}
.card-icon.blue{{background:#e7f0ff;color:#1a63d6}}
.card-icon.green{{background:#e5f8ee;color:#189a5a}}
.card-icon.purple{{background:#f2ebff;color:#7b3fe4}}
.card-body{{min-width:0}}
.card-label{{color:#1760d2;font-size:12px;font-weight:700;margin-bottom:5px}}
.card-value{{color:#172033;font-size:25px;font-weight:790;overflow-wrap:anywhere;line-height:1.2}}
.content{{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:20px;margin-top:20px}}
.chart-card,.insight{{background:white;border:1px solid #e7ebf2;border-radius:18px;padding:22px;box-shadow:0 8px 25px rgba(24,45,80,.06)}}
.chart-card{{min-height:535px}}
.chart-header{{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:10px}}
.chart-title{{font-size:18px;font-weight:750}}
.chart-subtitle{{color:#7a8495;font-size:12.5px;margin-top:4px}}
.toggle{{display:flex;background:#f0f3f8;padding:4px;border-radius:999px}}
.toggle button{{border:0;background:transparent;border-radius:999px;padding:7px 15px;font-size:12px;font-weight:700;cursor:pointer;color:#647087}}
.toggle button.active{{background:#2467dc;color:white;box-shadow:0 2px 7px rgba(26,99,214,.35)}}
#chart{{width:100%;height:450px}}
.insight{{height:fit-content}}
.insight-title{{font-size:17px;font-weight:750;margin-bottom:16px;display:flex;align-items:center;gap:9px}}
.insight-title svg{{width:19px;height:19px;color:#f5a623}}
.insight-highlight{{border-radius:13px;background:#edf5ff;padding:15px 16px;margin-bottom:16px}}
.insight-headline{{color:#12305e;font-size:14.5px;font-weight:700;line-height:1.5;margin-bottom:6px}}
.insight-text{{color:#4d5a72;line-height:1.6;font-size:13.5px}}
.insight-rows{{display:flex;flex-direction:column;gap:10px}}
.insight-row{{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid #eef1f6;border-radius:12px}}
.insight-row-icon{{flex:0 0 auto;width:34px;height:34px;border-radius:999px;display:flex;align-items:center;justify-content:center}}
.insight-row-icon svg{{width:16px;height:16px}}
.insight-row-icon.up{{background:#e5f8ee;color:#189a5a}}
.insight-row-icon.down{{background:#fdecec;color:#d3402f}}
.insight-row-icon.avg{{background:#e7f0ff;color:#1a63d6}}
.insight-row-value{{font-size:15px;font-weight:750;color:#182235;line-height:1.25}}
.insight-row-label{{font-size:11.5px;color:#828da0;font-weight:600}}
.footer{{color:#8993a3;font-size:11px;margin-top:16px;text-align:right}}
.accent-bar{{height:5px;border-radius:6px;margin-top:22px;background:linear-gradient(90deg,#1a63d6,#22c55e,#f5a623,#ec4899)}}
@media(max-width:1000px){{.content{{grid-template-columns:1fr}}.cards{{grid-template-columns:1fr}}}}
@media(max-width:700px){{.page{{padding:14px}}.header{{flex-direction:column;align-items:flex-start}}.metric-selector,.metric-display,.period-badge{{width:100%}}h1{{font-size:21px}}.chart-header{{flex-direction:column;align-items:flex-start}}}}
</style></head><body><div class="page">
<section class="header">
<div class="header-left"><div class="header-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></div><div><div class="eyebrow">Purchase Analytics</div><h1>{th}</h1></div></div>
<div class="header-right"><div class="period-badge"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg><div><div class="period-badge-label">Period</div><div class="period-badge-value">{ph}</div></div></div>{selector}<div class="topn-selector"><label>Show</label><select id="topNSelect" onchange="changeTopN(this.value)"><option value="5" selected>Top 5</option><option value="10">Top 10</option><option value="20">Top 20</option><option value="50">Top 50</option><option value="all">All</option></select></div></div>
</section>
<section class="cards">
<div class="card"><div class="card-icon blue"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></div><div class="card-body"><div class="card-label" id="totalLabel">Total {smh}</div><div class="card-value" id="totalValue">{total:,.2f}</div></div></div>
<div class="card"><div class="card-icon green"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 21h8"/><path d="M12 17v4"/><path d="M7 4h10v4a5 5 0 0 1-10 0V4z"/><path d="M7 6H4a3 3 0 0 0 3 4"/><path d="M17 6h3a3 3 0 0 1-3 4"/></svg></div><div class="card-body"><div class="card-label">Highest {dh}</div><div class="card-value" id="highestValue">{hih}</div></div></div>
<div class="card"><div class="card-icon purple"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></div><div class="card-body"><div class="card-label">Average {smh} by {dh}</div><div class="card-value" id="avgValue">{avgv}</div><div class="card-sub" id="avgSub">Across {len(initial)} values</div></div></div>
</section>
<section class="content">
<div class="chart-card"><div class="chart-header"><div><div class="chart-title" id="chartTitle">{smh} by {dh}</div><div class="chart-subtitle">Values are based on the Purchase data returned for this request.</div></div><div class="toggle"><button id="barBtn" class="active" onclick="renderChart('bar')">Bar</button><button id="lineBtn" onclick="renderChart('line')">Line</button></div></div><div id="chart"></div></div>
<aside class="insight">
<div class="insight-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/></svg>Key Insight</div>
<div class="insight-highlight"><div class="insight-headline" id="insightHeadline">{headline}</div><div class="insight-text" id="insightText">{ins}</div></div>
<div class="insight-rows">
<div class="insight-row"><div class="insight-row-icon up"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg></div><div><div class="insight-row-value" id="insightHighValue">{hiv}</div><div class="insight-row-label">Highest {dh} — <span id="insightHighest">{hih}</span></div></div></div>
<div class="insight-row"><div class="insight-row-icon down"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg></div><div><div class="insight-row-value" id="insightLowValue">{lov}</div><div class="insight-row-label">Lowest {dh} — <span id="insightLowest">{loh}</span></div></div></div>
<div class="insight-row"><div class="insight-row-icon avg"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></div><div><div class="insight-row-value" id="insightAvgValue">{avgv}</div><div class="insight-row-label">Average {smh} by {dh}</div></div></div>
</div>
</aside>
</section>
<div class="accent-bar"></div>
<div class="footer">Generated {gen}</div>
</div><script>
const metrics={sj};const metricData={dj};const dimensionName={json.dumps(dimension)};let currentMetric={json.dumps(selected_metric)};let currentChartType={json.dumps(chart_type)};
const chartPalette=['#2563D9','#21B9D5','#8B4DE8','#E83D9B','#19A963','#F3A51B','#EF5B4D','#11A9A0','#6366F1','#F07A24'];let topN=5;
function fmt(n){{return n.toLocaleString(undefined,{{minimumFractionDigits:2,maximumFractionDigits:2}})}}
function getDisplayedRows(metric){{const rows=[...(metricData[metric]||[])].sort((a,b)=>b.value-a.value);return topN==='all'?rows:rows.slice(0,Number(topN));}}
function updateSummary(metric){{const rows=metricData[metric]||[];const displayed=getDisplayedRows(metric);const total=rows.reduce((s,x)=>s+x.value,0);const hi=displayed.length?displayed[0]:null;const lo=displayed.length?displayed[displayed.length-1]:null;const displayedTotal=displayed.reduce((s,x)=>s+x.value,0);const avg=displayed.length?displayedTotal/displayed.length:0;
document.getElementById('totalLabel').textContent='Total '+metric;
document.getElementById('totalValue').textContent=fmt(total);
document.getElementById('avgValue').textContent=fmt(avg);
document.getElementById('avgSub').textContent=(topN==='all'?'Across all '+rows.length:'Across top '+displayed.length)+' '+dimensionName+' values';
document.getElementById('highestValue').textContent=hi?hi.category:'—';
document.getElementById('chartTitle').textContent=metric+' by '+dimensionName;
document.getElementById('insightHighest').textContent=hi?hi.category:'—';
document.getElementById('insightLowest').textContent=lo?lo.category:'—';
document.getElementById('insightHighValue').textContent=hi?fmt(hi.value):'—';
document.getElementById('insightLowValue').textContent=lo?fmt(lo.value):'—';
document.getElementById('insightAvgValue').textContent=fmt(avg);
document.getElementById('insightHeadline').textContent=(hi&&lo)?(hi.category+' recorded the highest '+metric+' at '+fmt(hi.value)+', while '+lo.category+' was the lowest at '+fmt(lo.value)+'.'):'No chartable business values were returned for this request.';
document.getElementById('insightText').textContent=!displayed.length?'No chartable business values were returned for this metric.':hi.category+' has the highest '+metric+' at '+fmt(hi.value)+'. The lowest displayed value is '+lo.category+' at '+fmt(lo.value)+'. The selected view contains '+displayed.length+' '+dimensionName+' values. Overall total across all returned data is '+fmt(total)+'.';
}}
function changeMetric(metric){{currentMetric=metric;updateSummary(metric);renderChart(currentChartType)}}
function changeTopN(value){{topN=value;updateSummary(currentMetric);renderChart(currentChartType)}}
function renderChart(type){{currentChartType=type;const rows=getDisplayedRows(currentMetric);const colors=type==='line'?chartPalette[0]:rows.map((_,i)=>chartPalette[i%chartPalette.length]);const trace={{x:rows.map(x=>x.category),y:rows.map(x=>x.value),type:type==='line'?'scatter':'bar',mode:type==='line'?'lines+markers':undefined,marker:{{color:colors,line:type==='line'?undefined:{{width:0}}}},line:type==='line'?{{color:chartPalette[0],width:3}}:undefined,hovertemplate:'%{{x}}<br>'+currentMetric+': %{{y:,.2f}}<extra></extra>'}};const layout={{margin:{{l:65,r:20,t:15,b:110}},paper_bgcolor:'white',plot_bgcolor:'white',font:{{family:'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'}},xaxis:{{title:dimensionName,tickangle:-35,automargin:true,gridcolor:'#edf0f5'}},yaxis:{{title:currentMetric,automargin:true,gridcolor:'#edf0f5',zeroline:true}},hoverlabel:{{bgcolor:'white',font:{{color:'#172033'}}}},showlegend:false}};Plotly.newPlot('chart',[trace],layout,{{responsive:true,displayModeBar:false}});document.getElementById('barBtn').classList.toggle('active',type==='bar');document.getElementById('lineBtn').classList.toggle('active',type==='line')}}
updateSummary(currentMetric);renderChart(currentChartType);
</script></body></html>'''


def generate_dashboard(*,question:str,columns:List[str],rows:List[Dict[str,Any]],intent:Any,date_range:Any=None,date_label:Optional[str]=None)->Dict[str,Any]:
 if not GRAPH_ENABLED:return {'generated':False,'reason':'Graph generation disabled.'}
 if not _graph_required(question,intent):return {'generated':False,'reason':'The requested Purchase result is not chartable.'}
 if not rows:return {'generated':False,'reason':'No Purchase data was returned.'}
 dimension_column=_dimension(columns,rows,question,intent)
 if not dimension_column:return {'generated':False,'reason':'No business dimension was found for the requested visualization.'}
 dimension_label=_business_dimension_label(getattr(intent,'group_by_column',None),dimension_column)
 metrics=_metrics(columns,rows,dimension_column)
 if not metrics:return {'generated':False,'reason':'No business metric was found in the returned Purchase data.'}
 selected_metric=_pick_metric(metrics,question,intent)
 if not selected_metric:return {'generated':False,'reason':'No business metric could be selected.'}
 extra_dimension_columns=_extra_dimension_columns(columns,dimension_column,metrics)
 dimension_columns=[dimension_column]+extra_dimension_columns
 if extra_dimension_columns:
  extra_labels=[_business_dimension_label(None,c) for c in extra_dimension_columns]
  dimension_label=dimension_label+' & '+' & '.join(extra_labels)
 metric_data={m:_chart_rows(rows,dimension_columns,m) for m in metrics}
 metric_data={m:v for m,v in metric_data.items() if v}
 if not metric_data:return {'generated':False,'reason':'The returned Purchase data has no chartable numeric values.'}
 if selected_metric not in metric_data:selected_metric=next(iter(metric_data))
 metrics=[m for m in metrics if m in metric_data]
 display_metric_data={};display_names=[];display_map={}
 for m in metrics:
  label=_business_metric_label(m,intent,question)
  if label in display_metric_data:label=m
  display_map[m]=label;display_names.append(label);display_metric_data[label]=metric_data[m]
 selected_label=display_map[selected_metric]
 chart_type=_chart_type(question,intent);period=_period_text(date_range,date_label)
 graph_id='purchase-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+uuid.uuid4().hex[:10]
 dashboard_html=_build_html(title=f'{selected_label} by {dimension_label}',period=period,dimension=dimension_label,metrics=display_names,selected_metric=selected_label,chart_type=chart_type,metric_data=display_metric_data)
 try:graph_url=_upload_html(graph_id,dashboard_html)
 except Exception as exc:
  if COS_REQUIRED:raise
  return {'generated':False,'graph_id':graph_id,'error':f'COS upload failed: {type(exc).__name__}: {exc}'}
 return {'generated':True,'graph_id':graph_id,'graph_url':graph_url,'dataset_type':'purchase','dimension':dimension_label,'metric':selected_label,'available_metrics':display_names,'chart_type':chart_type,'reporting_period':period,'row_count':len(rows),'chart_row_count':len(metric_data.get(selected_metric,[])),'payload':{'columns':columns,'rows':rows}}

def build_dashboard_from_result(*,question:str,columns:List[str],rows:List[Dict[str,Any]],intent:Any,date_range:Any=None,date_label:Optional[str]=None)->Dict[str,Any]:
 return generate_dashboard(question=question,columns=columns,rows=rows,intent=intent,date_range=date_range,date_label=date_label)
def warm_up_async()->None:return None