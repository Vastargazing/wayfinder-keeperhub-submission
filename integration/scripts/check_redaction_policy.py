#!/usr/bin/env python3
"""Compare the JSON-domain port with actual pinned KeeperHub TypeScript, offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

from keeperhub_executor.provenance import (
    KEEPERHUB_PIN, SENSITIVE_KEYS, redact_sensitive_data,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keeperhub', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    source = args.keeperhub / 'lib/utils/redact.ts'
    pinned = subprocess.check_output(['git', '-C', str(args.keeperhub), 'show',
                                     f'{KEEPERHUB_PIN}:lib/utils/redact.ts'])
    assert source.read_bytes() == pinned, 'redaction source differs from policy pin'
    keys = sorted(SENSITIVE_KEYS | {k.upper() for k in SENSITIVE_KEYS} | {
        'prefixCredentialSuffix', 'my_api-key-field', 'notsecretpublic', 'authCustom',
        'session_token_value', 'tokenAddress', 'asset', 'contractAddress', 'to'})
    values = ['', 'a', '1234', '12345', '1234567890123456', '😀😀😀', 'abc😀x',
              None, True, False, 0, 1, [], {'token': 'abcdef'}]
    corpus = [{key: value} for key in keys for value in values]
    for depth in range(14):
        item = {'token': 'abcdef'}
        for _ in range(depth):
            item = {'nested': [item]}
        corpus.append(item)
    code = r'''
const fs = require('node:fs'), vm = require('node:vm');
const {stripTypeScriptTypes} = require('node:module');
(async () => {
 const source = fs.readFileSync(process.argv[1], 'utf8');
 const mod = new vm.SourceTextModule(stripTypeScriptTypes(source));
 const logging = new vm.SyntheticModule(['ErrorCategory','logSystemError'], function() {
   this.setExport('ErrorCategory', {UNKNOWN:'unknown'});
   this.setExport('logSystemError', () => { throw Error('unexpected redaction exception'); });
 });
 await mod.link(name => { if (name !== '@/lib/logging') throw Error(name); return logging; });
 await mod.evaluate();
 const inputs = JSON.parse(fs.readFileSync(0, 'utf8'));
 process.stdout.write(JSON.stringify(inputs.map(x => mod.namespace.redactSensitiveData(x))));
})().catch(e => { process.stderr.write(String(e)); process.exit(1); });
'''
    proc = subprocess.run([os.environ.get('NODE_BIN', 'node'), '--experimental-vm-modules',
                           '-e', code, str(source)], input=json.dumps(corpus), text=True,
                          capture_output=True, check=True)
    actual = json.loads(proc.stdout)
    expected = [redact_sensitive_data(item) for item in corpus]
    assert expected == actual
    args.out.write_text(json.dumps({'pin': KEEPERHUB_PIN,
        'source': str(source), 'sourceSha256': hashlib.sha256(pinned).hexdigest(),
        'cases': len(corpus), 'matched': len(actual),
        'scope': 'JSON values; exact key Set, regex search, UTF-16, recursion boundary'}, indent=2)+'\n')
    print(f'{len(actual)}/{len(corpus)} TypeScript parity cases')


if __name__ == '__main__':
    main()
