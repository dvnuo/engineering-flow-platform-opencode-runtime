import json

def safe_load(text):
    text=text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        out={}
        for line in text.splitlines():
            if ':' in line:
                k,v=line.split(':',1)
                out[k.strip()]=v.strip().strip('"\'')
        return out

def safe_dump(obj, sort_keys=False, allow_unicode=True):
    return json.dumps(obj, ensure_ascii=not allow_unicode, indent=2, sort_keys=sort_keys)
