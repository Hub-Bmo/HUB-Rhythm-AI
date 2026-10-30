"""Offline, auditable rhythm-excerpt preparation. Never changes source projects.

This produces provisional agent-reviewed excerpts, not a claim of human approval
or a reconstructed concert performance. Unresolved cells remain in the audit.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score_recognizer.workspace import Workspace, atomic_json, now
from score_recognizer.contract import PPQ, meter_length, fraction
from score_reviewer.document import instrument_record
from score_reviewer.review_workflow import training_mask, inferred_profile
from score_reviewer.table_export import make_tables, xlsx_bytes, Table
from score_reviewer.timeline import tie_pitch_signature
from score_tokenizer.core import validate_source

NAMES = ['黃土情', '黃土高坡', '天下無雙', '闖將令']
ALIASES = {'方响': '方響', '小镲': '小鈸', '小钹': '小鈸', '大钹': '大鈸',
           '大镲':'大鈸', '小锣':'小鑼', '大锣':'大鑼',
           '金属串': '金屬串', '铃鼓': '鈴鼓', 'Timp': '定音鼓', 'Timp.': '定音鼓'}
KNOWN = set('定音鼓 方響 小鈸 大鈸 吊鈸 軍鈸 小鼓 大鼓 小堂鼓 大堂鼓 木魚 金屬串 鈴鼓 小鑼 大鑼 三角鐵 班鼓 板鼓 文鑼 低音鼓 高音堂鼓'.split())
HTC_RESTS = {8:4,18:16,54:9,63:4,88:4,92:5,133:4,141:28,142:15,164:6,171:4,174:4}
MAIN_RESTS = {2:3,4:6,11:17,32:2,34:2,38:2,40:3,42:9,51:4,76:6,77:6,
              118:4,130:44,140:3,141:8,142:6,143:5,145:5,147:5,154:2}
TIMP_RESTS = {169:13,176:29,200:45,201:4,205:23,228:4,236:40,254:7,255:6,262:4,265:4,272:2}
ALT_TWO_BAR_REPEATS = {n+i:n-2+i for n in [311,313,320,329,331,333,335,339,341,343,345,349,
                                                          356,358,360,364,366,368,370] for i in (0,1)}


def rat(value):
    value = Fraction(value)
    return {'n': value.numerator, 'd': value.denominator}


def step_value(points, number):
    return next(value for first, value in reversed(points) if first <= number)


def numbering(name, old):
    """Return variant, output ID, printed measure, compressed span, exclusion."""
    if name == '黃土情':
        actual = old + sum(count-1 for start,count in HTC_RESTS.items() if start < old)
        return '定音鼓與兼奏', actual, actual, HTC_RESTS.get(old,1), None
    if name != '黃土高坡':
        return '總譜', old, old, 1, None
    if old <= 168:
        if 5 <= old <= 8:
            return '打擊分譜A', old, None, 1, '此列實際五格而登記四格，且辨識失敗；保留原圖，不推定節奏'
        if 125 <= old <= 128:
            return '打擊分譜A', old, None, 1, '與121–124為同一原譜列，重複框選，避免重複訓練'
        cursor = 1
        for n in range(1,old):
            if n == 5:
                cursor += 5
            if 5 <= n <= 8 or 125 <= n <= 128:
                continue
            cursor += MAIN_RESTS.get(n,1)
        reason = '笛子裝飾奏，打擊聲部未提供可量化節奏' if old == 141 else None
        return '打擊分譜A', cursor, cursor, MAIN_RESTS.get(old,1), reason
    if old <= 287:
        actual = old-168 + sum(count-1 for start,count in TIMP_RESTS.items() if start < old)
        reason = '笛子裝飾奏，打擊聲部未提供可量化節奏' if old == 254 else None
        return '定音鼓分譜', 1000+actual, actual, TIMP_RESTS.get(old,1), reason
    # An alternative handwritten copy: no unproven alignment with version A.
    return '打擊分譜B（不同抄本）', 2000+old-287, None, 1, None


def meter_for(name, old, explicit=None):
    if explicit:
        return explicit
    if name == '天下無雙':
        return '4/4'
    if name == '闖將令':
        return '4/4' if old < 13 or old >= 228 else '2/4'
    if name == '黃土情':
        return step_value([(1,'4/4'),(11,'2/4'),(63,'3/4'),(91,'2/4'),
                           (133,'3/4'),(136,'2/4'),(141,'6/8'),(164,'3/4'),
                           (165,'2/4'),(170,'4/4'),(185,'2/4')], old)
    return step_value([(1,'4/4'),(7,'2/4'),(76,'3/4'),(77,'2/4'),(118,'3/4'),
                       (121,'2/4'),(130,'6/8'),(142,'3/4'),(143,'2/4'),(144,'4/4'),
                       (156,'2/4'),(169,'4/4'),(170,'2/4'),(201,'3/4'),(202,'2/4'),
                       (228,'3/4'),(229,'2/4'),(236,'6/8'),(255,'3/4'),(256,'2/4'),
                       (261,'4/4'),(275,'2/4'),(288,'4/4'),(294,'2/4'),(351,'3/4'),
                       (353,'2/4'),(392,'3/4'),(395,'2/4'),(400,'6/8'),(412,'3/4'),
                       (418,'4/4'),(423,'2/4')],old)


def exact_name(text):
    text = (text or '').strip()
    text = ALIASES.get(text,text)
    return text if text in KNOWN else None


def split_voice(part, event):
    text = part['staff_label'] + ' ' + event.get('raw_symbol','')
    if any(s in text for s in ['上聲部','上行聲部','上行符幹','符幹向上','上符幹','上層','上列']):
        return '小鈸'
    if any(s in text for s in ['下聲部','下行聲部','下行符幹','符幹向下','下符幹','下層','下列']):
        return '小鼓'
    # Only separate clearly distinct vertical symbol bands in this same part.
    ys = sorted(e['bbox']['y'] for e in part['events'] if e['kind']=='note')
    if len(ys)>1:
        gap,at = max((ys[i+1]-ys[i],i) for i in range(len(ys)-1))
        if gap >= .12:
            return '小鈸' if event['bbox']['y'] < (ys[at]+ys[at+1])/2 else '小鼓'
    return None


def instrument_for(name, old, part, event):
    direct = exact_name(event.get('instrument_text')) or exact_name(part.get('instrument_text')) or exact_name(part['staff_label'])
    if name == '天下無雙':
        row = re.fullmatch(r'第([1-5])列',part['staff_label'])
        if row:return ['小鈸','小鑼','大鑼','小鼓','大鼓'][int(row[1])-1]
        return '大鑼' if direct=='大鈸' else direct
    if name == '黃土高坡' and 169 <= old <= 287:
        return '定音鼓'
    # In these confirmed two-voice passages the stem/voice defines the drum;
    # a model's generic 小鼓 label must not turn the upper cymbal voice into it.
    paired = name=='黃土情' and any(a<=old<=b for a,b in [(11,16),(24,26),(33,34),(61,62),(117,140)])
    paired |= name=='黃土高坡' and any(a<=old<=b for a,b in [(9,10),(18,19),(26,27),(50,50),(102,117),(119,129),(156,168)])
    if paired and ('下譜' in part['staff_label'] or '下方' in part['staff_label'] or name=='黃土高坡'):
        voice=split_voice(part,event)
        if voice:return voice
    if direct:
        return direct
    label = part['staff_label']
    if name == '天下無雙':
        return exact_name(label)
    if name == '闖將令':
        y = part['bbox']['y']
        if '定音鼓' in label or (('上方五線' in label or '最上方五線' in label or '第1列' in label) and y < .26):
            return '定音鼓'
        if '五線譜' in label and y < .28 and '下方' not in label:return '定音鼓'
        if any(p.get('clef')=='bass' for p in event.get('pitches',[])) and y < .3:
            return '定音鼓'
        if .25 <= y < .46 or (('上方單線' in label or '第一條單線' in label or '單線聲部1' in label) and y < .55):
            return '大鈸' if old >= 228 else '小鈸'
        if 180 <= old < 228 and .46 <= y < .7:
            return '小鼓'
        return None
    if name == '黃土情':
        top = (part['bbox']['y'] < .45 and '下方' not in label and '下譜' not in label)
        if top:
            return '金屬串' if 36 <= old < 88 else '定音鼓'
        if old <= 10: return '方響'
        if any(a<=old<=b for a,b in [(11,16),(24,26),(33,34),(61,62),(117,132),(136,140)]):
            return split_voice(part,event)
        if any(a<=old<=b for a,b in [(19,23),(27,32),(55,60)]): return '木魚'
        if 64 <= old < 117: return '鈴鼓'
        if 143 <= old < 185: return '大鈸'
        if old >= 185:return '小鼓'
        return None
    if old <= 168:
        if old in (1,3): return '方響'
        if any(a<=old<=b for a,b in [(9,10),(18,19),(26,27),(50,50),(102,117),(119,129),(156,168)]):
            return split_voice(part,event)
        if any(a<=old<=b for a,b in [(12,17),(20,25),(43,49)]): return '木魚'
        if 29 <= old <= 42: return '金屬串'
        if 52 <= old <= 101: return '鈴鼓'
        if 131 <= old <= 155: return '大鈸'
    if old >= 288:
        if old <= 293:return '方響'
        if any(a<=old<=b for a,b in [(294,299),(307,308),(315,316),(324,325),(375,391),(393,399),(424,436)]):
            return split_voice(part,event)
        if any(a<=old<=b for a,b in [(301,306),(309,314),(318,323)]):return '木魚'
        if 327 <= old <= 374:return '鈴鼓'
        if 401 <= old <= 423:return '大鈸'
    return None


def raw_duration(raw):
    if re.search('疑|可能|不清|模糊|？|看似',raw):
        return None
    types=[('三十二分',Fraction(1,8)),('十六分',Fraction(1,4)),
           ('八分',Fraction(1,2)),('四分',Fraction(1)),('二分',Fraction(2)),('全音符',Fraction(4))]
    if sum(text in raw for text,_ in types)>1:return None
    for text,duration in types:
        if text in raw:
            return duration * (Fraction(3,2) if '附點' in raw else 1)
    return None


def repair_timing(events, meter):
    """Recover explicit note-value labels, then a single complete serial voice."""
    result = deepcopy(events)
    for e in result:
        if e['kind'] not in {'note','rest'}:
            continue
        if e.get('duration') is None:
            duration = raw_duration(e.get('raw_symbol',''))
            if duration is not None:
                e['duration'] = rat(duration)
                e.setdefault('preparation_changes',[]).append('依原辨識的明確音符名稱補回時值')
    serial = [e for e in result if e['kind'] in {'note','rest'}]
    if serial and len(serial)==len(result) and all(e.get('duration') is not None for e in serial):
        total = sum((fraction(e['duration']) for e in serial),Fraction())
        if total == meter_length(meter):
            cursor = Fraction()
            proposals=[]
            for e in serial:
                if e.get('onset') is not None and fraction(e['onset']) != cursor:
                    break
                proposals.append((e,cursor));cursor+=fraction(e['duration'])
            else:
                for e,onset in proposals:
                    if e.get('onset') is None:
                        e['onset']=rat(onset)
                        e.setdefault('preparation_changes',[]).append('完整單聲部時值恰好滿小節，依音符順序補回起拍')
    return result


def is_repeat(part):
    if len(part['events']) != 1 or part['events'][0]['kind'] != 'unreadable':
        return False
    e=part['events'][0];raw=e.get('raw_symbol','');text=raw+' '.join(e.get('uncertainties',[]))
    if any(t in text for t in ['兩小節','雙小節','二小節','兩道','雙斜']): return False
    return '%' in raw or '％' in raw or ('斜線' in raw and ('點' in raw or '前一小節' in text)) or ('小節反覆' in raw or '小節重複' in raw)


def mask_unproven_ties(group):
    """A readable note head is usable even when its tie pairing is unproven.

    Keep the recognized tie text and mask only its supervision. In particular,
    an unmatched continuation must never become a newly asserted attack.
    """
    active={};cursor=Fraction()
    for cell in group:
        for e in sorted(cell['events'],key=lambda e:fraction(e['onset'])):
            key=(e['instrument_id'],e.get('technique_id'),tie_pitch_signature(e))
            onset=cursor+fraction(e['onset']);end=onset+fraction(e['duration']);tie=e.get('tie')
            e['original_tie_before_excerpt_mask']=tie
            if tie in ['continue','stop']:
                previous=active.get(key)
                if not previous or previous[0]!=onset:
                    e['training_mask']['tie']=e['training_mask']['attack']=False;e['attack']=None
                    e['provenance']['tie_pairing']='unproven_in_excerpt'
                elif tie=='stop':
                    del active[key]
                else:
                    active[key]=(end,previous[1])
            if tie=='start':
                if key in active:
                    active[key][1]['training_mask']['tie']=False
                    e['training_mask']['tie']=False
                active[key]=(end,e)
        cursor+=meter_length(cell['time_signature'])
    for _,start in active.values():
        start['training_mask']['tie']=False


class Catalog:
    def __init__(self): self.items={}
    def get(self,name):
        if name not in self.items:
            profile=inferred_profile(name) or ('bell' if name in ['方響','金屬串','木魚','三角鐵'] else 'drum')
            self.items[name]=instrument_record(name,profile,name in ['方響','定音鼓'])
        return self.items[name]
    def instrument(self,id): return next((v for v in self.items.values() if v['instrument_id']==id),None)
    def technique(self,ins,stroke):
        record=self.instrument(ins)
        return next((s for s in (record or {}).get('strokes',[]) if s['technique_id']==stroke),None)


def current_or_same_crop(workspace,region):
    run=workspace.latest_run(region)
    if run and run['status'] in {'succeeded','empty'}: return run,False
    # A changed confirmation or metadata can hide an already paid-for exact crop.
    for old in reversed(workspace.package['recognition_runs']):
        s=old.get('source_snapshot') or {}
        if old['region_id'] != region['region_id'] or old['status'] != 'succeeded':continue
        if all(s.get(k)==region.get(k) for k in ['asset_id','bbox','boundaries','start_bar','bar_count']):
            return old,True
    return run,False


def build(name, root=ROOT):
    folder=root/'projects'/(name+'.score-recognition');path=folder/'recognition-package.json';original_bytes=path.read_bytes()
    package=json.loads(original_bytes.decode('utf-8'));w=Workspace(folder,package);catalog=Catalog()
    assets={a['asset_id']:a for a in package['assets']};cells=[];candidates=[];audit=[];counters=Counter();staff_history={};cell_history={};supplements=[]
    existing_path=root/'projects'/(name+'.score-review')/'review-project.json'
    existing=json.loads(existing_path.read_text(encoding='utf-8')) if existing_path.exists() else {}
    edits={e['id']:e for e in existing.get('entries',[])}
    navigation=[]
    for region in sorted(package['regions'],key=lambda r:r['start_bar']):
        run,recovered=current_or_same_crop(w,region)
        measures={m['index']:m for m in (run or {}).get('result',{}).get('measures',[])} if (run or {}).get('result') else {}
        observations=defaultdict(list)
        for obs in (run or {}).get('observations',[]): observations[obs['bar']].append(obs)
        navigation.extend(deepcopy(((run or {}).get('result') or {}).get('navigation',[])))
        asset=assets[region['asset_id']]
        for index in range(region['bar_count']):
            old=region['start_bar']+index;variant,number,printed,span,exclusion=numbering(name,old)
            source={'page':asset['printed_page_number'],'asset_id':asset['asset_id'],'source_filename':asset['original_filename'],
                    'relative_image_path':str((folder/asset['relative_path']).resolve()),'region_id':region['region_id'],
                    'registration_bar':old,'printed_bar':printed,'variant':variant,'compressed_span':span,
                    'region_bbox':deepcopy(region['bbox']),'bar_bbox':deepcopy(region['bbox'])}
            left,right=region['boundaries'][index:index+2];source['bar_bbox']['x']+=left*source['bar_bbox']['width'];source['bar_bbox']['width']*=right-left
            measure=measures.get(index,{});meter=meter_for(name,old,measure.get('time_signature') or region.get('time_signature'))
            original_obs=deepcopy(observations[old]);candidates.append({'source':source,'source_registration_bar':old,'result':deepcopy(measure),'observations':original_obs,'run_id':(run or {}).get('run_id'),'engine':(run or {}).get('engine','openai'),'model':(run or {}).get('model'),'exclusion':exclusion})
            counters['registered_cells']+=1
            if exclusion:
                audit.append({'source':source,'reason':exclusion,'scope':'cell','category':'duplicate_source' if '重複框選' in exclusion else 'unresolved_layout'});counters['excluded_cells']+=1;continue
            if not run or run['status'] not in {'succeeded','empty'} or index not in measures:
                audit.append({'source':source,'reason':'沒有可沿用的成功辨識結果','scope':'cell','category':'missing_recognition'});counters['excluded_cells']+=1;continue
            if span>1:
                # Expand the source index for numbering, but do not fabricate full-ensemble silence.
                audit.append({'source':source,'reason':f'多小節休止／無定量段共 {span} 小節，已恢復後續編號；不作敲擊訓練片段','scope':'cell','category':'verified_rest_numbering'});counters['compressed_cells']+=1;continue
            cell={'number':number,'time_signature':meter,'tempo_quarter':measure.get('tempo_quarter'),
                  'source':source,'events':[],'rest_observations':[],'rest_evidence':[],
                  'coverage':{'complete':True,'blank_placeholders_removed':0,'human_verified':False}}
            if name=='黃土高坡' and old in ALT_TWO_BAR_REPEATS:
                previous_number=2000+ALT_TWO_BAR_REPEATS[old]-287
                previous=cell_history.get((variant,previous_number))
                if previous and previous['time_signature']==meter:
                    cell['events']=deepcopy(previous['events'])
                    for ei,e in enumerate(cell['events']):
                        e['expanded_from_event_id']=e['event_id'];e.update(event_id=f'two-repeat:{old}:{ei}',bar=number,
                            raw_symbol='兩小節反覆：'+e.get('raw_symbol',''),source_asset_id=asset['asset_id'],source_bbox=deepcopy(source['bar_bbox']))
                        e['provenance']={**e.get('provenance',{}),'repeat_marker_registration_bar':old,'expanded_from_output_bar':previous_number}
                    counters['expanded_repeat_cells']+=1
                    cell_history[(variant,number)]=deepcopy(cell)
                    if cell['events']:cells.append(cell)
                    continue
            problems=[];safe_parts=[]
            for pi,part in enumerate(measure.get('parts',[])):
                events=repair_timing(part.get('events',[]),meter)
                prototype={'kind':'note','bbox':part['bbox'],'raw_symbol':'','pitches':[]}
                part_ins=instrument_for(name,old,part,prototype)
                if part_ins=='金屬串' and len(events)==2 and '箭頭' in events[0].get('raw_symbol',''):
                    rest=events[1]
                    if rest['kind']=='rest' and rest.get('onset') and rest.get('duration') and fraction(rest['onset'])==1 and fraction(rest['duration'])==1 and meter_length(meter)==2:
                        events[0].update(kind='note',onset=rat(0),duration=rat(1))
                        events[0].setdefault('preparation_changes',[]).append('箭頭奏法後明示第二拍休止，將動作定位於第一拍；技法仍保留原文')
                history_key=(variant,part_ins) if part_ins else None
                if is_repeat(part) and history_key and history_key in staff_history:
                    previous,previous_meter,previous_number=staff_history[history_key]
                    if previous_meter==meter and previous_number==number-1:
                        events=deepcopy(previous)
                        for ei,e in enumerate(events):
                            e['expanded_from_event_id']=e.get('event_id');e['event_id']=f"repeat:{number}:{part_ins}:{ei}";e['bar']=number
                            e['source_asset_id']=asset['asset_id'];e['source_bbox']=deepcopy(source['bar_bbox']);e['raw_symbol']='同前小節：'+e.get('raw_symbol','')
                            e['provenance']={**e.get('provenance',{}),'repeat_marker_registration_bar':old,'expanded_from_output_bar':previous_number}
                        cell['events'].extend(events);staff_history[history_key]=(deepcopy(events),meter,number);counters['expanded_repeat_cells']+=1;continue
                part_notes=[];part_bad=False
                for ei,e in enumerate(events):
                    match=next((o for o in original_obs if o.get('staff_label')==part['staff_label'] and o.get('bbox')==e.get('bbox') and o.get('raw_symbol','')==e.get('raw_symbol','')),None)
                    observation_id=(match or {}).get('observation_id') or f"{run['run_id']}:{old}:{pi}:{ei}"
                    manual=edits.get(observation_id)
                    if manual and manual['status']=='rejected': continue
                    value={**deepcopy(e),'bar':number,'staff_label':part['staff_label'],'source_asset_id':asset['asset_id']}
                    if manual and manual['value'] != manual.get('original'):
                        value=deepcopy(manual['value']);value['bar']=number;counters['preserved_existing_edits']+=1
                    if value['kind']=='unreadable':
                        if not value.get('raw_symbol'):
                            cell['coverage']['blank_placeholders_removed']+=1;counters['blank_placeholders']+=1
                            continue
                        problems.append(f"{part['staff_label']}：未解析記號 {value.get('raw_symbol')}");part_bad=True;continue
                    if value['kind']=='rest':
                        cell['rest_observations'].append({'evidence_id':observation_id,'kind':'rest','scope':'source_staff','staff_label':part['staff_label'],
                            'instrument_text':part_ins,'onset':value.get('onset'),'duration':value.get('duration'),'timing_known':value.get('onset') is not None and value.get('duration') is not None,
                            'source_asset_id':asset['asset_id'],'source_bbox':deepcopy((match or {}).get('source_bbox',source['bar_bbox']))})
                        continue
                    if value['kind']!='note':continue
                    insname=instrument_for(name,old,part,value)
                    if manual and exact_name(manual['value'].get('instrument_text')):insname=exact_name(manual['value']['instrument_text'])
                    if not insname:
                        problems.append(f"{part['staff_label']}：樂器尚不能可靠對應");part_bad=True;continue
                    try:
                        on,dur=fraction(value.get('onset')),fraction(value.get('duration'),positive=True)
                        if on>=meter_length(meter) or on+dur>meter_length(meter) or (on*PPQ).denominator!=1 or (dur*PPQ).denominator!=1:raise ValueError()
                    except (ValueError,TypeError,KeyError):
                        problems.append(f"{insname}：起拍／時值未知或超出小節");part_bad=True;continue
                    ins=catalog.get(insname);tech=value.get('technique_text')
                    basic=tech in (None,'','一般敲擊','基本擊法','一般','普通敲擊','tr','輪奏','滾奏','震音')
                    value.update(instrument_id=ins['instrument_id'],instrument_text=insname,
                                 technique_id=ins['strokes'][0]['technique_id'] if basic else None,
                                 event_id=observation_id,observation_id=observation_id,run_id=run['run_id'],kind='note',review_status='accepted',
                                 source_bbox=deepcopy((match or {}).get('source_bbox',source['bar_bbox'])))
                    if not ins['pitched']:
                        value['original_model_pitches']=deepcopy(value.get('pitches',[]));value['pitches']=[]
                    value['provenance']={'reviewer':'Codex','review_level':'provisional_agent_structural_review','human_verified':manual is not None and manual['status']=='accepted',
                                         'model':run.get('model'),'engine':run.get('engine','openai'),'source_registration_bar':old,'exact_crop_history_recovered':recovered}
                    for flag in ['is_roll','is_accent','is_ornament']:
                        if type(value.get(flag)) is not bool:value[flag]=None
                    uncertainty=' '.join(value.get('uncertainties',[]))
                    for flag,words in [('is_roll',['輪奏','滾奏']),('is_accent',['重音']),('is_ornament',['裝飾音'])]:
                        if any(word in uncertainty for word in words) and any(word in uncertainty for word in ['不清','無法','不確定','不能確定']):value[flag]=None
                    if value.get('tie') not in ['none','start','continue','stop']:value['tie']='unknown'
                    value['training_mask']=training_mask(catalog,value)
                    value['attack']=value['tie'] in ['none','start'] if value['training_mask']['tie'] else None
                    part_notes.append(value)
                if part_bad:
                    if history_key:staff_history.pop(history_key,None)
                elif part_notes:
                    safe_parts.append((part['staff_label'],deepcopy(part_notes)))
                    # History is only usable for one fully timed known instrument.
                    instruments={e['instrument_text'] for e in part_notes}
                    if len(instruments)==1:staff_history[(variant,next(iter(instruments)))]=(deepcopy(part_notes),meter,number)
                cell['events'].extend(part_notes)
                if part.get('status')=='full_rest':
                    rid=f"{run['run_id']}:{old}:fullrest:{pi}";cell['rest_evidence'].append(rid)
                    cell['rest_observations'].append({'evidence_id':rid,'kind':'full_rest','scope':'source_staff','staff_label':part['staff_label'],
                                                      'instrument_text':part_ins,'timing_known':True,'source_asset_id':asset['asset_id'],'source_bbox':deepcopy(source['bar_bbox'])})
            if problems:
                counters['excluded_cells']+=1
                audit.append({'source':source,'scope':'cell','category':'unresolved_events','reason':'；'.join(dict.fromkeys(problems)),'timed_candidate_notes':len(cell['events'])})
                # Never turn omitted unknown strikes into silent negative targets.
                for staff,notes in safe_parts:
                    sn=len(supplements)+1
                    for e in notes:
                        e['ensemble_output_bar']=e['bar'];e['bar']=sn
                        if e.get('tie') in ['start','continue','stop']:
                            e['training_mask']['tie']=False
                            if e['tie'] in ['continue','stop']:e['training_mask']['attack']=False;e['attack']=None
                    supplements.append({'number':sn,'time_signature':meter,'tempo_quarter':cell['tempo_quarter'],
                        'source':{**deepcopy(source),'staff_scope':staff},'events':notes,'rest_observations':[],
                        'coverage':{'scope':'one_source_staff_only','other_staffs':'unknown_not_silent','human_verified':False}})
                continue
            if not cell['events']:
                cell_history[(variant,number)]=deepcopy(cell)
                counters['silent_or_empty_cells']+=1;continue
            # Duplicate model observations at one source location are not extra strikes.
            seen=set();unique=[]
            for e in cell['events']:
                key=(e['instrument_id'],json.dumps(e['onset'],sort_keys=True),json.dumps(e['duration'],sort_keys=True),json.dumps(e.get('source_bbox'),sort_keys=True),json.dumps(e.get('pitches'),sort_keys=True))
                if key not in seen:seen.add(key);unique.append(e)
                else:counters['duplicate_observations_removed']+=1
            cell['events']=unique;cell_history[(variant,number)]=deepcopy(cell);cells.append(cell)
    cells.sort(key=lambda c:c['number'])
    numbers=[c['number'] for c in cells]
    assert len(numbers)==len(set(numbers)),'Duplicate prepared bar IDs'
    # Short paper-order excerpts avoid inventing D.S./Coda routes or musical sections.
    segments=[];groups=[]
    for cell in cells:
        if (not groups or len(groups[-1])>=8 or cell['number']!=groups[-1][-1]['number']+1
                or cell['source']['variant']!=groups[-1][-1]['source']['variant']
                or cell['time_signature']!=groups[-1][-1]['time_signature']):groups.append([cell])
        else:groups[-1].append(cell)
    selected=set(numbers)
    for i,group in enumerate(groups,1):
        segment_id=f"AUTO-{i:03}";order=[{'source_bar':c['number'],'pass_index':1} for c in group]
        # A cropped boundary cannot prove the missing side of a tie: mask it,
        # retaining the original candidate, rather than inventing a fresh attack.
        mask_unproven_ties(group)
        segments.append({'segment_id':segment_id,'name':f"{group[0]['source']['variant']}｜片段{i:03}",
                         'start_bar':group[0]['number'],'end_bar':group[-1]['number'],'playback_order':order,
                         'segmentation_method':'automatic_source_order_max_8_bars','musical_section_verified':False,
                         'event_count':sum(len(c['events']) for c in group),'verified_silent':False})
    payload={'format':'musicai-reviewed-training','schema_version':'1.0','ppq':PPQ,
             'review_id':f"URGENT-{package['package_id']}",'title':name,'exported_at':now(),
             'source_package_id':package['package_id'],'segments':segments,'instruments':list(catalog.items.values()),
             'navigation':[],'navigation_scopes':[],'navigation_evidence':navigation,
             'playback_order_mode':'paper_order_excerpts','playback_order':[x for s in segments for x in s['playback_order']],
             'bars':cells,'annotation_policy':{'mode':'rhythm','validation_status':'provisional_agent_review','human_verified':False,
                 'training_requirement':'Only use fields with training_mask=true; null is not false.',
                 'unknown_fields':'preserve_and_mask','musical_segments':'automatic excerpts, not human musical sections',
                 'performance_route':'not expanded; individual source excerpts only','tempo_policy':'null is unknown; disable tempo tokenization',
                 'variants':'separate excerpts; never overlay uncertain duplicate part versions'},
             'preparation_summary':dict(counters),'source_manifest_sha256':sha256(original_bytes).hexdigest()}
    validate_source(payload)
    assert path.read_bytes()==original_bytes,'Source was changed during offline preparation'
    return payload,{'title':name,'candidates':candidates,'pending_review':audit,'navigation_evidence':navigation,'single_staff_supplements':supplements,
                    'summary':dict(counters),'numbering_note':'registration_bar is the old app cell number; printed_bar is the reconstructed printed measure when supported.'}


def audit_workbook(payload,audit,path):
    tables=make_tables(payload);bars={b['number']:b for b in payload['bars']}
    for table in tables:
        table.headers=list(table.headers)
        table.rows=[list(row) for row in table.rows]
        if '原譜小節' not in table.headers:continue
        index=table.headers.index('原譜小節')
        table.headers+=['分譜／版本','辨識程式原登記格','已核對的紙面小節']
        for row in table.rows:
            source=bars[row[index]]['source']
            row.extend([source['variant'],source['registration_bar'],source['printed_bar']])
    lookup={r['source']['registration_bar']:r for r in audit['pending_review']}
    selected={b['source']['registration_bar'] for b in payload['bars']}
    mapping=[]
    for candidate in audit['candidates']:
        s=candidate['source'];old=s['registration_bar'];item=lookup.get(old,{})
        status='主訓練檔已納入' if old in selected else item.get('category','無敲擊／未列訓練片段')
        mapping.append([s['source_filename'],s['page'],s['variant'],old,s['printed_bar'],s['compressed_span'],status,item.get('reason','')])
    tables.append(Table('來源與改號對照',['來源檔案','頁碼','分譜／版本','原登記格','紙面小節','涵蓋小節數','處理狀態','說明'],mapping))
    tables.append(Table('待複核位置',['來源檔案','原登記格','紙面小節','類型','原因'],[
        [r['source']['source_filename'],r['source']['registration_bar'],r['source']['printed_bar'],r['category'],r['reason']]
        for r in audit['pending_review'] if r['category'] not in ['verified_rest_numbering','duplicate_source']]))
    tables.append(Table('暫行版說明',['項目','內容'],[
        ['核對等級','原譜頁面／聲部脈絡核對及結構檢查；尚未逐音人工複核，不是最終黃金標註'],
        ['範圍','按紙面順序切成最多八小節片段；沒有猜測 D.S.／Coda 的完整演奏路線'],
        ['速度','未確認速度保留未知，TOKEN 化時請關閉速度欄位'],
        ['編號','輸出ID為唯一索引；分譜跨版本以額外編號區間區分，實際紙面小節另列'],
        ['未知','未知不是沒有，請遵守 JSON training_mask；待複核格不當作無聲訓練'],
        ['來源','原辨識專案未修改；完整候選另存，所有被排除資料可追溯']]))
    path.write_bytes(xlsx_bytes(tables))


def main():
    out=ROOT/'outputs'/'四份樂譜_20261029_暫行訓練版';out.mkdir(parents=True,exist_ok=True)
    (out/'訓練JSON').mkdir(exist_ok=True);(out/'完整候選與待複核').mkdir(exist_ok=True);(out/'閱讀表格').mkdir(exist_ok=True)
    (out/'單聲部補充JSON').mkdir(exist_ok=True)
    summary=[]
    for name in NAMES:
        payload,audit=build(name)
        atomic_json(out/'訓練JSON'/f'{name}.json',payload);atomic_json(out/'完整候選與待複核'/f'{name}_完整候選.json',audit)
        if payload['segments']:
            audit_workbook(payload,audit,out/'閱讀表格'/f'{name}.xlsx')
        supplemental=deepcopy(payload);supplemental['title']=name+'｜單聲部補充';supplemental['review_id']+='-single-staff'
        supplemental['bars']=deepcopy(audit['single_staff_supplements']);supplemental['segments']=[];supplemental['playback_order']=[]
        supplemental['annotation_policy']['scope']='one source staff per sample; omitted staffs are unknown, not silent'
        for bar in supplemental['bars']:
            n=bar['number'];order=[{'source_bar':n,'pass_index':1}]
            supplemental['segments'].append({'segment_id':f'STAFF-{n:03}','name':bar['source']['staff_scope']+f'｜補充{n:03}',
                'start_bar':n,'end_bar':n,'playback_order':order,'event_count':len(bar['events']),
                'instrument_scope':sorted({e['instrument_text'] for e in bar['events']}),'musical_section_verified':False})
            supplemental['playback_order'].extend(order)
        if supplemental['segments']:
            validate_source(supplemental);atomic_json(out/'單聲部補充JSON'/f'{name}_單聲部補充.json',supplemental)
        record={'title':name,'bars':len(payload['bars']),'events':sum(len(c['events']) for c in payload['bars']),
                'segments':len(payload['segments']),'pending_cells':sum(r['category'] not in ['verified_rest_numbering','duplicate_source'] for r in audit['pending_review']),
                'supplemental_staff_samples':len(supplemental['bars']),'supplemental_events':sum(len(b['events']) for b in supplemental['bars']),**payload['preparation_summary']}
        summary.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    atomic_json(out/'資料清單.json',summary)
    print(out)


if __name__=='__main__':main()
