#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import os
os.chdir(os.path.dirname(__file__))
sys.path.insert(0, 'api')

from dotenv import load_dotenv
load_dotenv()

import os
print('1. GOOGLE_API_KEY 있음:', bool(os.environ.get('GOOGLE_API_KEY')))

from config import LLM_ENABLED
print('2. LLM_ENABLED:', LLM_ENABLED)

from llm import llm_status, compile_policy
print('3. LLM Status:', llm_status())

# 테스트
if LLM_ENABLED:
    print('\n=== JSON 변환 테스트 ===')
    result = compile_policy('하루에 100만원까지 송금 가능')
    print('입력: 하루에 100만원까지 송금 가능')
    print('출처:', result.get('source'))
    print('일일한도:', result.get('daily_limit'))
    print('월간한도:', result.get('monthly_limit'))
    
    print('\n=== 테스트 2 ===')
    result2 = compile_policy('신규 계좌로 송금 ㄴㄴ')
    print('입력: 신규 계좌로 송금 ㄴㄴ')
    print('출처:', result2.get('source'))
    print('결과:', result2)
else:
    print('\n⚠️ LLM이 비활성화되어 있습니다')
