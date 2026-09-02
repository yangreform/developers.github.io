import os, json
env_path = 'op.env'
env_config = {}
with open(env_path, 'r', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '=' in line:
            k, v = line.split('=', 1)
            env_config[k.strip()] = v.strip().strip('\'').strip('\"')

print('SEND_WEBHOOK:', env_config.get('SEND_WEBHOOK'))
try:
    j = json.loads(env_config.get('HEDGE_CONFIG_JSON', '{}'))
    print('JSON IS VALID:', j.keys())
except Exception as e:
    print('JSON ERROR:', e)
    print('RAW JSON:', env_config.get('HEDGE_CONFIG_JSON'))
