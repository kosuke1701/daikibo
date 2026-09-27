"""Optional provider configuration; native CLI login is the default, no proxy."""
from pathlib import Path
from urllib.parse import urlsplit
from .common import atomic_write, canonical, digest, need, parse_json, text, timestamp

class Providers:
    def __init__(self,c): self.c=c; self.s=c.s
    def configure(self,actor,name,kind,base_url,secret,models=None,max_requests=256):
        actor.require('owner'); text(name); text(secret)
        need(kind in {'anthropic','openai'},'invalid_provider','Unknown provider')
        u=urlsplit(base_url)
        need(u.scheme in {'http','https'} and u.netloc,'invalid_provider','Expected HTTP(S) URL')
        path=self.s.home/'provider-secrets'/digest(name.encode()); atomic_write(path,secret.encode())
        body={'name':name,'kind':kind,'base_url':base_url.rstrip('/'),'models':models or [],
              'max_requests':max_requests,'request_limit_enforced':False,'connection':'direct'}
        with self.s.transaction():
            self.s.execute('INSERT INTO providers VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET body=excluded.body,secret_path=excluded.secret_path,created=excluded.created',
                           (name,canonical(body).decode(),str(path),timestamp()))
            self.c.sec.event(None,'provider_configured',actor.id,body)
        return body
    def list(self,actor):
        return {'default':'native_cli_login','providers':[parse_json(r['body']) for r in self.s.all('SELECT body FROM providers')]}
    def remove(self,actor,name):
        actor.require('owner'); row=self.s.one('SELECT secret_path FROM providers WHERE name=?',(name,),True)
        with self.s.transaction(): self.s.execute('DELETE FROM providers WHERE name=?',(name,))
        Path(row['secret_path']).unlink(missing_ok=True); return {'removed':name}
    def environment(self,adapter):
        if not adapter.get('provider'): return {}
        row=self.s.one('SELECT * FROM providers WHERE name=?',(adapter['provider'],),True)
        body=parse_json(row['body']); secret=Path(row['secret_path']).read_text()
        if adapter['kind']=='claude':
            need(body['kind']=='anthropic','provider_mismatch','Claude requires Anthropic configuration')
            return {'ANTHROPIC_API_KEY':secret,'ANTHROPIC_BASE_URL':body['base_url']}
        need(body['kind']=='openai','provider_mismatch','Codex requires OpenAI configuration')
        return {'OPENAI_API_KEY':secret,'OPENAI_BASE_URL':body['base_url']}
    def close(self): pass
