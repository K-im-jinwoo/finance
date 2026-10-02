"""Local-only child process for testing the PowerShell input pipe. No network."""
import argparse
import json
import sys

parser=argparse.ArgumentParser()
parser.add_argument('--mode',choices=['CLIENT_CREDENTIALS','ACCESS_TOKEN'],required=True)
parser.add_argument('--reject',action='store_true')
args=parser.parse_args()
raw=sys.stdin.buffer.read()
assert not raw.startswith(b'\xef\xbb\xbf'), 'UTF-8 must not include a BOM'
assert raw.endswith(b'\n'), 'stdin must finish with the JSON line and EOF'
payload=json.loads(raw.decode('utf-8'))
expected={'client_id':'SYNTHETIC-ID-\ud14c\uc2a4\ud2b8','client_secret':'SYNTHETIC-SECRET-\u00e9'} if args.mode=='CLIENT_CREDENTIALS' else {'access_token':'SYNTHETIC-TOKEN'}
assert payload==expected, 'stdin values did not survive UTF-8 transport'
assert not any(value in '\n'.join(sys.argv) for value in expected.values()), 'credentials must not be command-line arguments'
print('SYNTHETIC-PRIVATE-STDERR',file=sys.stderr)
if args.reject:
    print(json.dumps({'error':'SHARED_OPERATING_CLIENT_FORBIDDEN','credential_values_returned':False}))
    raise SystemExit(1)
print(json.dumps({'status':'LOCAL_PIPE_TEST_PASSED','credential_values_returned':False}))
