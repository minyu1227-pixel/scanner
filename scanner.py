"""
scanner.py
──────────────────────────────────────────────────────────────
MACD(5, 20, 5) 골든크로스 + 이격도(15 SMA) 과매도 이력 스캐너
(TradingView Pine Script "MACD 골든크로스 + 이격도 과매도 이력" 로직 이식)

대상 종목 : 파일 안의 TICKERS 리스트 (전체 스캔 시 get_universe()는 호출하지 않음)
1차 조건  : 최근 종가 >= 30 달러
2차 조건  : 최근 20거래일 평균 거래대금(종가 × 거래량) >= 300억 원
시그널    : ① 최근 봉에서 MACD 골든크로스
            ② 최근 봉을 제외한 직전 10개 봉 중 이격도 <= 85 인 봉 존재

텔레그램  : 환경변수 TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 가 있으면 실행 결과를 전송
            (결과와 상관없이 실행 1회당 최소 1개 메시지, 오류 발생 시 오류 메시지)

필요 패키지 : pip install yfinance pandas numpy
사용 예     : python scanner.py                       (전체 스캔 → 오늘 시그널 전송)
              python scanner.py --codes TSLA AAPL     (지정 종목의 과거 시그널 일자 전송)
              python scanner.py --codes TSLA,NVDA --max-signals 20
              python scanner.py --limit 300            (테스트용: 앞 300종목만)
              python scanner.py --include-today        (장중 미완성 봉 포함)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd
import yfinance as yf

# ───── 전제 조건 (Pine Script 입력값과 동일) ─────
MACD_FAST = 5
MACD_SLOW = 20
MACD_SIGNAL = 5
DISP_LEN = 15          # 이격도 SMA 기간
DISP_TH = 85.0         # 이격도 과매도 기준 (이하)
LOOKBACK = 10          # 과매도 확인 구간 (오늘 제외 봉 수)

# ───── 종목 필터 조건 ─────
MIN_PRICE_USD = 30.0                 # 1차: 주가 30달러 이상
MIN_TURNOVER_KRW = 30_000_000_000    # 2차: 평균 거래대금 300억 원 이상
TURNOVER_DAYS = 20                   # 거래대금 평균 기간
FALLBACK_USDKRW = 1400.0             # 환율 조회 실패 시 사용할 값

# ───── 종목 리스트 (NASDAQ Trader 공식 심볼 디렉터리) ─────
#   HTTPS가 봇 차단(Incapsula 503)되는 경우가 있어 같은 파일의 공식 FTP로 자동 대체
SYMDIR_SOURCES = [
    "https://www.nasdaqtrader.com/dynamic/SymDir/{}",
    "ftp://ftp.nasdaqtrader.com/symboldirectory/{}",
]
OTHER_EXCHANGES = {"N": "NYSE", "A": "AMEX"}   # A = NYSE American(구 AMEX)

# 보통주가 아닌 증권을 걸러내기 위한 키워드
EXCLUDE_RE = re.compile(
    r"\b(preferred|warrants?|rights?|units?|depositary|notes?|debentures?|"
    r"subordinated|trust preferred)\b",
    re.IGNORECASE,
)

TELEGRAM_MAX_LEN = 4000   # 텔레그램 메시지 한도(4096자)보다 약간 작게

# ───── 전체 스캔 대상 종목 (get_universe() 대신 이 리스트를 사용) ─────
TICKERS = [
    'A', 'AA', 'AAMI', 'AAOI', 'AAON', 'AAPL', 'ABBV', 'ABCB', 'ABG', 'ABNB', 'ABT', 'ACA', 'ACGL',
    'ACHC', 'ACIW', 'ACLS', 'ACM', 'ACMR', 'ADBE', 'ADC', 'ADI', 'ADM', 'ADP', 'ADSK', 'ADUS', 'AEE',
    'AEHR', 'AEIS', 'AEM', 'AEP', 'AFG', 'AFL', 'AFRM', 'AGCO', 'AGIO', 'AGL', 'AGM', 'AGO', 'AGX',
    'AGYS', 'AHR', 'AIG', 'AIN', 'AIR', 'AIT', 'AIZ', 'AJG', 'AKAM', 'ALAB', 'ALB', 'ALG', 'ALGM',
    'ALGN', 'ALGT', 'ALK', 'ALL', 'ALLY', 'ALNT', 'ALNY', 'ALRM', 'ALSN', 'ALV', 'AMAT', 'AMBQ', 'AMD',
    'AMG', 'AMGN', 'AMKR', 'AMP', 'AMR', 'AMSC', 'AMT', 'AMZN', 'AN', 'ANAB', 'ANDE', 'ANET', 'ANF',
    'AOS', 'APA', 'APAM', 'APD', 'APG', 'APGE', 'APH', 'APO', 'APP', 'APPF', 'AR', 'ARCB', 'ARE',
    'ARES', 'ARMK', 'AROC', 'ARW', 'ARWR', 'ARXS', 'ASB', 'ASH', 'ASO', 'ASTH', 'ASTS', 'ATEX', 'ATI',
    'ATKR', 'ATMU', 'ATO', 'ATR', 'ATRC', 'ATRO', 'AUB', 'AVA', 'AVAV', 'AVB', 'AVGO', 'AVNT', 'AVT',
    'AVY', 'AWI', 'AWK', 'AWR', 'AX', 'AXGN', 'AXON', 'AXP', 'AXS', 'AXSM', 'AXTI', 'AYI', 'AZO',
    'BA', 'BAC', 'BAH', 'BALL', 'BAND', 'BANR', 'BAP', 'BATRK', 'BBIO', 'BBY', 'BC', 'BCC', 'BCO',
    'BCPC', 'BDC', 'BDX', 'BE', 'BELFA', 'BELFB', 'BEN', 'BFAM', 'BFH', 'BH', 'BHE', 'BHF', 'BIIB',
    'BILL', 'BIO', 'BJ', 'BJRI', 'BKH', 'BKNG', 'BKR', 'BKU', 'BL', 'BLBD', 'BLDR', 'BLFS', 'BLK',
    'BLKB', 'BLLN', 'BMI', 'BMO', 'BMRN', 'BMY', 'BNY', 'BOH', 'BOKF', 'BOOT', 'BOX', 'BP', 'BPOP',
    'BR', 'BRKR', 'BRO', 'BROS', 'BSX', 'BSY', 'BTI', 'BTSG', 'BURL', 'BUSE', 'BWA', 'BWXT', 'BX',
    'BXP', 'BYD', 'BZH', 'C', 'CACC', 'CACI', 'CAH', 'CAKE', 'CALM', 'CALX', 'CAR', 'CARG', 'CARR',
    'CART', 'CASY', 'CAT', 'CATY', 'CAVA', 'CB', 'CBC', 'CBRE', 'CBRL', 'CBRS', 'CBSH', 'CBT', 'CBZ',
    'CCI', 'CCJ', 'CDNA', 'CDNL', 'CDNS', 'CDW', 'CE', 'CECO', 'CEG', 'CELC', 'CENX', 'CEVA', 'CF',
    'CFG', 'CFR', 'CG', 'CGNX', 'CHD', 'CHDN', 'CHEF', 'CHH', 'CHRD', 'CHRW', 'CHTR', 'CI', 'CIEN',
    'CINF', 'CL', 'CLH', 'CLMT', 'CLS', 'CLX', 'CM', 'CMC', 'CME', 'CMG', 'CMI', 'CMS', 'CNA',
    'CNC', 'CNI', 'CNK', 'CNM', 'CNMD', 'CNO', 'CNP', 'CNQ', 'CNR', 'CNX', 'COCO', 'COF', 'COGT',
    'COHR', 'COHU', 'COIN', 'COKE', 'COLB', 'COLM', 'CON', 'COO', 'COP', 'COR', 'CORT', 'COST', 'CPA',
    'CPAY', 'CPK', 'CPT', 'CR', 'CRAI', 'CRBG', 'CRC', 'CRCL', 'CRI', 'CRL', 'CRM', 'CRNX', 'CROX',
    'CRS', 'CRUS', 'CRWD', 'CRWV', 'CSCO', 'CSL', 'CSW', 'CSX', 'CTAS', 'CTRE', 'CTSH', 'CTVA', 'CUBI',
    'CURB', 'CVCO', 'CVI', 'CVLT', 'CVNA', 'CVS', 'CVX', 'CW', 'CWEN', 'CWST', 'CWT', 'CXT', 'CXW',
    'CYTK', 'D', 'DAL', 'DAR', 'DASH', 'DAVE', 'DB', 'DBX', 'DCI', 'DCO', 'DD', 'DDOG', 'DDS',
    'DE', 'DECK', 'DELL', 'DEO', 'DFNS', 'DG', 'DGII', 'DGX', 'DHI', 'DHR', 'DINO', 'DIOD', 'DIS',
    'DJCO', 'DK', 'DKS', 'DLB', 'DLR', 'DLTR', 'DNTH', 'DOCN', 'DOCU', 'DORM', 'DOV', 'DOW', 'DPZ',
    'DRI', 'DRS', 'DSGX', 'DT', 'DTE', 'DTM', 'DUK', 'DUOL', 'DVA', 'DVN', 'DXCM', 'DXPE', 'DY',
    'EAT', 'EBAY', 'ECG', 'ECHO', 'ECL', 'ECO', 'ECPG', 'ED', 'EEFT', 'EFX', 'EG', 'EGP', 'EHC',
    'EIX', 'EL', 'ELF', 'ELS', 'ELV', 'ELVN', 'EMBJ', 'EME', 'EMN', 'EMR', 'ENB', 'ENPH', 'ENS',
    'ENSG', 'ENTG', 'ENVA', 'EOG', 'EPAM', 'EPD', 'EPR', 'EPRT', 'EQH', 'EQIX', 'EQT', 'ERIE', 'ES',
    'ESAB', 'ESE', 'ESI', 'ESS', 'ETON', 'ETR', 'ETSY', 'EVR', 'EVRG', 'EW', 'EWBC', 'EWTX', 'EXC',
    'EXE', 'EXEL', 'EXLS', 'EXP', 'EXPD', 'EXPE', 'EXPO', 'EXR', 'FAF', 'FANG', 'FAST', 'FBIN', 'FBRX',
    'FCFS', 'FCN', 'FCNCA', 'FCX', 'FDS', 'FDX', 'FDXF', 'FE', 'FEIM', 'FELE', 'FERG', 'FFBC', 'FFIN',
    'FFIV', 'FHI', 'FIBK', 'FICO', 'FIS', 'FISV', 'FITB', 'FIVE', 'FIX', 'FLR', 'FLS', 'FMX', 'FND',
    'FNF', 'FORM', 'FOUR', 'FOX', 'FOXA', 'FPS', 'FR', 'FRPT', 'FRT', 'FSLR', 'FSS', 'FTAI', 'FTDR',
    'FTNT', 'FTV', 'FUL', 'FWONK', 'G', 'GATX', 'GBCI', 'GD', 'GDDY', 'GE', 'GEF', 'GEHC', 'GEV',
    'GFF', 'GGG', 'GH', 'GHC', 'GIB', 'GIL', 'GILD', 'GIS', 'GKOS', 'GL', 'GLPI', 'GLW', 'GM',
    'GMED', 'GNRC', 'GOLF', 'GOOGL', 'GPC', 'GPI', 'GPN', 'GRAL', 'GRMN', 'GS', 'GSAT', 'GSHD', 'GTLB',
    'GVA', 'GWRE', 'GWW', 'GXO', 'H', 'HAE', 'HAL', 'HALO', 'HAS', 'HASI', 'HCA', 'HCC', 'HCI',
    'HD', 'HEI', 'HGV', 'HHH', 'HIG', 'HII', 'HIW', 'HLI', 'HLIO', 'HLNE', 'HLT', 'HMC', 'HNGE',
    'HNI', 'HOMB', 'HON', 'HONA', 'HOOD', 'HP', 'HPE', 'HQY', 'HRB', 'HRI', 'HRMY', 'HROW', 'HSBC',
    'HSIC', 'HSY', 'HTO', 'HUBB', 'HUBG', 'HUBS', 'HUM', 'HURN', 'HUT', 'HWC', 'HWKN', 'HWM', 'HXL',
    'HZO', 'IBKR', 'IBM', 'IBOC', 'IBP', 'ICE', 'ICUI', 'IDA', 'IDCC', 'IDXX', 'IDYA', 'IESC', 'IEX',
    'IFF', 'ILMN', 'IMAX', 'IMO', 'IMVT', 'INCY', 'INDB', 'INDV', 'ING', 'INGR', 'INOD', 'INSM', 'INSP',
    'INSW', 'INTA', 'INTC', 'INTU', 'INVH', 'IOND', 'IONQ', 'IONS', 'IOT', 'IP', 'IPAR', 'IPGP', 'IQV',
    'IR', 'IRDM', 'IRM', 'IRON', 'IRTC', 'ISRG', 'IT', 'ITGR', 'ITRI', 'ITT', 'ITW', 'IVT', 'IVZ',
    'J', 'JAN', 'JAZZ', 'JBHT', 'JBL', 'JBTM', 'JEF', 'JJSF', 'JKHY', 'JLL', 'JNJ', 'JPM', 'JXN',
    'KAI', 'KALU', 'KBH', 'KBR', 'KDP', 'KEX', 'KEYS', 'KFY', 'KGS', 'KKR', 'KLAC', 'KLIC', 'KMB',
    'KMI', 'KMT', 'KN', 'KNF', 'KNSL', 'KNTK', 'KO', 'KOD', 'KR', 'KRC', 'KRMN', 'KRYS', 'KTB',
    'KTOS', 'KWR', 'KYMR', 'L', 'LAD', 'LAMR', 'LASR', 'LAUR', 'LAZ', 'LBRDK', 'LDOS', 'LEA', 'LEN',
    'LEU', 'LFUS', 'LGND', 'LH', 'LHX', 'LIF', 'LII', 'LIND', 'LINE', 'LITE', 'LLY', 'LLYVK', 'LMAT',
    'LMB', 'LMND', 'LMT', 'LNC', 'LNG', 'LNT', 'LNTH', 'LOAR', 'LOPE', 'LOW', 'LPG', 'LPLA', 'LPX',
    'LQDA', 'LRCX', 'LRN', 'LSCC', 'LSTR', 'LTH', 'LTM', 'LULU', 'LUV', 'LVS', 'LW', 'LXP', 'LYV',
    'MA', 'MAA', 'MAIN', 'MAIR', 'MAN', 'MANE', 'MANH', 'MAR', 'MAS', 'MATX', 'MBX', 'MC', 'MCD',
    'MCHP', 'MCK', 'MCO', 'MCY', 'MDB', 'MDGL', 'MDLN', 'MDLZ', 'MEDP', 'MELI', 'MEOH', 'MET', 'META',
    'MFC', 'MFP', 'MGA', 'MGM', 'MHK', 'MHO', 'MIAX', 'MIDD', 'MIRM', 'MKC', 'MKL', 'MKSI', 'MKTX',
    'MLI', 'MLM', 'MMM', 'MMS', 'MMSI', 'MNPR', 'MOD', 'MOH', 'MORN', 'MP', 'MPC', 'MPTI', 'MPWR',
    'MRCY', 'MRK', 'MRNA', 'MRSH', 'MRVL', 'MS', 'MSA', 'MSCI', 'MSFT', 'MSGE', 'MSGS', 'MSI', 'MSM',
    'MSTR', 'MTB', 'MTCH', 'MTD', 'MTDR', 'MTG', 'MTH', 'MTN', 'MTSI', 'MTZ', 'MU', 'MUR', 'MUSA',
    'MXL', 'MYRG', 'MZTI', 'NATL', 'NBIX', 'NDAQ', 'NDSN', 'NEE', 'NET', 'NEU', 'NFG', 'NFLX', 'NHC',
    'NHI', 'NI', 'NIC', 'NJR', 'NKE', 'NKTR', 'NNN', 'NOC', 'NOVT', 'NOW', 'NP', 'NPO', 'NRG',
    'NSC', 'NSIT', 'NSP', 'NTAP', 'NTCT', 'NTNX', 'NTRA', 'NTRS', 'NUE', 'NUTX', 'NVDA', 'NVEC', 'NVO',
    'NVR', 'NVS', 'NWE', 'NWPX', 'NWS', 'NXPI', 'NXST', 'NXT', 'NYT', 'O', 'OC', 'ODFL', 'OGE',
    'OGS', 'OHI', 'OII', 'OKE', 'OKTA', 'OLED', 'OLLI', 'OMC', 'OMCL', 'OMF', 'ON', 'ONTO', 'OPLN',
    'ORA', 'ORCL', 'ORI', 'ORKA', 'ORLY', 'OSIS', 'OSK', 'OTIS', 'OUST', 'OUT', 'OXY', 'OZK', 'PAAS',
    'PACS', 'PAG', 'PANW', 'PARR', 'PATK', 'PAY', 'PAYC', 'PAYX', 'PB', 'PBF', 'PBH', 'PCAR', 'PCOR',
    'PCTY', 'PCVX', 'PDFS', 'PECO', 'PEG', 'PEGA', 'PEN', 'PENG', 'PEP', 'PFG', 'PFGC', 'PFSI', 'PG',
    'PGR', 'PH', 'PHIN', 'PHM', 'PI', 'PII', 'PIPR', 'PJT', 'PKG', 'PLAB', 'PLD', 'PLNT', 'PLPC',
    'PLTR', 'PLUS', 'PLXS', 'PM', 'PNC', 'PNW', 'PODD', 'POOL', 'POR', 'POST', 'POWI', 'POWL', 'PPG',
    'PPL', 'PPLI', 'PRAX', 'PRDO', 'PRG', 'PRI', 'PRIM', 'PRK', 'PRKS', 'PRM', 'PRU', 'PSA', 'PSMT',
    'PSN', 'PSX', 'PTC', 'PTCT', 'PTGX', 'PVH', 'PVLA', 'PWR', 'PYPL', 'Q', 'QCOM', 'QLYS', 'QNT',
    'QRVO', 'QTWO', 'R', 'RAL', 'RAMP', 'RBA', 'RBC', 'RBLX', 'RBRK', 'RCI', 'RCL', 'RDDT', 'RDN',
    'RDNT', 'REG', 'REGN', 'REXR', 'RF', 'RGA', 'RGEN', 'RGLD', 'RH', 'RHI', 'RIO', 'RJF', 'RKLB',
    'RL', 'RLI', 'RMBS', 'RMD', 'RNG', 'RNR', 'RNST', 'ROAD', 'ROG', 'ROK', 'ROKU', 'ROL', 'ROP',
    'ROST', 'RPM', 'RRC', 'RRR', 'RRX', 'RS', 'RSG', 'RTX', 'RUSHA', 'RVMD', 'RVTY', 'RY', 'RYAN',
    'RYTM', 'SAFT', 'SAH', 'SAIA', 'SAIC', 'SAM', 'SANM', 'SBAC', 'SBCF', 'SBUX', 'SCCO', 'SCHW', 'SCI',
    'SEB', 'SEDG', 'SEI', 'SEIC', 'SEZL', 'SF', 'SFBS', 'SFM', 'SGI', 'SHAK', 'SHAZ', 'SHOO', 'SHW',
    'SIGI', 'SITE', 'SITM', 'SJM', 'SKM', 'SKT', 'SKWD', 'SKY', 'SKYW', 'SLAB', 'SLF', 'SLG', 'SLGN',
    'SM', 'SMA', 'SMCI', 'SMG', 'SMTC', 'SNA', 'SNDK', 'SNDR', 'SNEX', 'SNOW', 'SNPS', 'SNX', 'SO',
    'SOLS', 'SOLV', 'SON', 'SPB', 'SPCX', 'SPG', 'SPGI', 'SPHR', 'SPSC', 'SPXC', 'SQM', 'SR', 'SRE',
    'SRRK', 'SSB', 'SSD', 'SSNC', 'STAG', 'STEP', 'STM', 'STN', 'STOK', 'STRA', 'STRL', 'STT', 'STZ',
    'SU', 'SUI', 'SUNB', 'SUPN', 'SWK', 'SWKS', 'SWX', 'SXI', 'SXT', 'SYF', 'SYK', 'SYM', 'SYNA',
    'SYRE', 'SYY', 'TAP', 'TARS', 'TBBK', 'TCBI', 'TD', 'TDG', 'TDW', 'TDY', 'TEAM', 'TECH', 'TEM',
    'TENB', 'TER', 'TEX', 'TFC', 'TFX', 'TGT', 'TGTX', 'THC', 'THO', 'THRM', 'TIGO', 'TJX', 'TKO',
    'TKR', 'TLN', 'TM', 'TMDX', 'TMO', 'TMUS', 'TNET', 'TOL', 'TOST', 'TOWN', 'TPB', 'TPC', 'TPG',
    'TPL', 'TPR', 'TREX', 'TRGP', 'TRMB', 'TRNO', 'TROW', 'TRP', 'TRU', 'TRV', 'TSCO', 'TSLA', 'TSN',
    'TTAN', 'TTC', 'TTEK', 'TTMI', 'TTWO', 'TVTX', 'TW', 'TWLO', 'TWST', 'TXG', 'TXN', 'TXNM', 'TXRH',
    'TXT', 'TYL', 'U', 'UAL', 'UBER', 'UBSI', 'UCB', 'UCTT', 'UDR', 'UFPI', 'UFPT', 'UGI', 'UHS',
    'UI', 'ULS', 'ULTA', 'UMBF', 'UNF', 'UNFI', 'UNH', 'UNM', 'UNP', 'UPS', 'URBN', 'URI', 'USB',
    'USFD', 'UTHR', 'VAC', 'VC', 'VCEL', 'VCTR', 'VCYT', 'VECO', 'VEEV', 'VERA', 'VIAV', 'VICR', 'VIRT',
    'VKTX', 'VLO', 'VLTO', 'VMC', 'VMI', 'VNO', 'VNOM', 'VNT', 'VOYA', 'VOYG', 'VPG', 'VRNS', 'VRSK',
    'VRSN', 'VRT', 'VRTS', 'VRTX', 'VSAT', 'VSEC', 'VSH', 'VSNT', 'VST', 'VSXY', 'VTR', 'VVV', 'VVX',
    'VZ', 'W', 'WAB', 'WAFD', 'WAL', 'WAT', 'WBS', 'WCC', 'WDAY', 'WDC', 'WDFC', 'WEC', 'WELL',
    'WERN', 'WFC', 'WGS', 'WH', 'WHD', 'WHR', 'WING', 'WK', 'WKC', 'WLK', 'WLY', 'WM', 'WMB',
    'WMS', 'WMT', 'WRB', 'WRLD', 'WSBC', 'WSFS', 'WSM', 'WSO', 'WST', 'WTFC', 'WTM', 'WTRG', 'WTS',
    'WWD', 'XEL', 'XMTR', 'XOM', 'XPO', 'XYL', 'XYZ', 'YETI', 'YOU', 'YUMC', 'ZBH', 'ZBRA', 'ZD',
    'ZG', 'ZION', 'ZM', 'ZS', 'ZTS', 'ZWS'
]


# ════════════════════════════════════════════════════════════
# 0. 텔레그램 (표준 라이브러리 urllib만 사용)
# ════════════════════════════════════════════════════════════
def _split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """줄 단위로 끊어서 텔레그램 길이 제한 안으로 분할."""
    chunks, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:              # 한 줄이 너무 긴 경우
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks or [text[:limit]]


def signal_keyboard(ticker: str) -> dict:
    """시그널 메시지 아래에 붙일 인라인 키보드 (✅ 승인 → buy:티커, ⏭ 패스 → pass:티커)."""
    return {"inline_keyboard": [[
        {"text": "✅ 승인", "callback_data": f"buy:{ticker}"},
        {"text": "⏭ 패스", "callback_data": f"pass:{ticker}"},
    ]]}


def send_telegram(text: str, reply_markup: dict | None = None) -> bool:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[텔레그램] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없어 전송을 건너뜁니다.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    ok = True
    chunks = _split_message(text)
    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": "true",
        }
        if reply_markup and idx == len(chunks) - 1:   # 버튼은 마지막 조각에만
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    body = json.loads(r.read().decode("utf-8"))
                if not body.get("ok"):
                    print(f"[텔레그램] 전송 실패: {body.get('description')}")
                    ok = False
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")
                print(f"[텔레그램] HTTP {e.code}: {detail}")
                if e.code == 429 and attempt < 2:      # 요청 과다 → 잠시 후 재시도
                    time.sleep(3 * (attempt + 1))
                    continue
                ok = False
                break
            except Exception as e:                      # 네트워크 오류 등
                print(f"[텔레그램] 전송 오류(재시도 {attempt + 1}/3): {type(e).__name__}")
                if attempt == 2:
                    ok = False
                time.sleep(3 * (attempt + 1))
    if ok:
        print("[텔레그램] 전송 완료")
    return ok


# ════════════════════════════════════════════════════════════
# 1. 종목 유니버스
# ════════════════════════════════════════════════════════════
def _fetch_symdir_text(filename: str) -> str:
    errors = []
    for tmpl in SYMDIR_SOURCES:
        url = tmpl.format(filename)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", errors="replace")
            if "|" in text.splitlines()[0]:          # 정상적인 파이프 구분 파일인지 확인
                return text
            errors.append(f"{url}: 예상치 못한 응답")
        except Exception as e:
            errors.append(f"{url}: {e}")
        print(f"  [경고] 종목 리스트 다운로드 실패 → 다음 경로 시도 ({errors[-1]})")
    raise RuntimeError(f"{filename} 다운로드 실패: " + " / ".join(errors))


def _read_symdir(filename: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(_fetch_symdir_text(filename)), sep="|", dtype=str)
    # 마지막 줄 "File Creation Time: ..." 제거
    return df[~df.iloc[:, 0].str.startswith("File Creation Time", na=False)]


def _is_common_stock(name: str) -> bool:
    return "common stock" in name.lower() and not EXCLUDE_RE.search(name)


def get_universe() -> pd.DataFrame:
    nq = _read_symdir("nasdaqlisted.txt")
    nq = nq[(nq["Test Issue"] == "N") & (nq["ETF"] == "N")]
    nq = nq.assign(Exchange="NASDAQ")[["Symbol", "Security Name", "Exchange"]]

    ot = _read_symdir("otherlisted.txt")
    ot = ot[(ot["Test Issue"] == "N") & (ot["ETF"] == "N")
            & ot["Exchange"].isin(OTHER_EXCHANGES.keys())]
    ot = ot.rename(columns={"ACT Symbol": "Symbol"})
    ot["Exchange"] = ot["Exchange"].map(OTHER_EXCHANGES)
    ot = ot[["Symbol", "Security Name", "Exchange"]]

    df = pd.concat([nq, ot], ignore_index=True)
    df = df[df["Security Name"].fillna("").map(_is_common_stock)]

    df["Ticker"] = df["Symbol"].map(to_yf_ticker)
    df = df[~df["Ticker"].str.contains(r"[\$=\^\s]", regex=True)]
    return df.drop_duplicates("Ticker").reset_index(drop=True)


def to_yf_ticker(symbol: str) -> str:
    """yfinance 표기로 변환 (BRK.B → BRK-B)."""
    return re.sub(r"[./]", "-", symbol.strip().upper())


def get_company_name(ticker: str) -> str:
    """yfinance에서 회사명 조회 (시그널 종목에만 호출). 실패하면 빈 문자열."""
    try:
        info = yf.Ticker(ticker).info
        return str(info.get("shortName") or info.get("longName") or "").strip()
    except Exception:
        return ""


def short_name(name: str) -> str:
    """'Tesla, Inc. - Common Stock' → 'Tesla, Inc.'"""
    return re.sub(r"\s*-?\s*(Class [A-Z] )?Common Stock.*$", "", str(name), flags=re.I).strip() or str(name)


# ════════════════════════════════════════════════════════════
# 2. 지표 계산 (Pine Script와 동일한 방식)
# ════════════════════════════════════════════════════════════
def pine_ema(s: pd.Series, length: int) -> pd.Series:
    """Pine ta.ema: 첫 값은 SMA(length)로 시드, 이후 alpha = 2/(length+1)."""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    valid = np.flatnonzero(~np.isnan(v))
    if len(valid) < length:
        return pd.Series(out, index=s.index)
    start = valid[0]
    seed = start + length - 1
    out[seed] = v[start:seed + 1].mean()
    alpha = 2.0 / (length + 1)
    for i in range(seed + 1, len(v)):
        x = v[i]
        out[i] = out[i - 1] if np.isnan(x) else alpha * x + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=s.index)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["Close", "Volume"])
    return df[df["Volume"] > 0]


def compute_indicators(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd = pine_ema(close, MACD_FAST) - pine_ema(close, MACD_SLOW)   # MACD(5, 20)
    signal = pine_ema(macd, MACD_SIGNAL)                              # Signal(5)
    disparity = close / close.rolling(DISP_LEN).mean() * 100          # 이격도(15 SMA)
    return macd, signal, disparity


def evaluate(df: pd.DataFrame, min_turnover_usd: float) -> dict | None:
    """가장 최근 봉 기준으로 조건 판정 (전체 스캔용)."""
    df = _clean(df)
    need = max(MACD_SLOW + MACD_SIGNAL + 2, DISP_LEN + LOOKBACK + 1, TURNOVER_DAYS)
    if len(df) < need:
        return None

    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    last_close = close.iloc[-1]
    avg_turnover = (close * volume).iloc[-TURNOVER_DAYS:].mean()

    macd, signal, disparity = compute_indicators(close)

    # ① ta.crossover(macd, signal): 오늘 macd > signal, 어제 macd <= signal
    golden_cross = bool(macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2])

    # ② ta.lowest(disparity, 10)[1] <= 85 : 오늘 제외 직전 10개 봉의 최저 이격도
    min_disp = disparity.iloc[-LOOKBACK - 1:-1].min()
    was_oversold = bool(min_disp <= DISP_TH)

    return {
        "Date": df.index[-1].date(),
        "Close": round(last_close, 2),
        "AvgTurnoverUSD": avg_turnover,
        "Disparity": round(disparity.iloc[-1], 2),
        "MinDisp10": round(min_disp, 2),
        "MACD": round(macd.iloc[-1], 4),
        "Signal": round(signal.iloc[-1], 4),
        "Pass1_Price": bool(last_close >= MIN_PRICE_USD),
        "Pass2_Turnover": bool(avg_turnover >= min_turnover_usd),
        "GoldenCross": golden_cross,
        "WasOversold": was_oversold,
    }


def find_signal_dates(df: pd.DataFrame, min_turnover_usd: float) -> pd.DataFrame:
    """전체 기간에서 시그널이 발생한 모든 봉 (종목 지정 조회용)."""
    df = _clean(df)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    macd, signal, disparity = compute_indicators(close)

    golden_cross = (macd > signal) & (macd.shift(1) <= signal.shift(1))
    min_disp = disparity.rolling(LOOKBACK).min().shift(1)
    hit = golden_cross & (min_disp <= DISP_TH)

    turnover = (close * volume).rolling(TURNOVER_DAYS).mean()
    out = pd.DataFrame({
        "Close": close.round(2),
        "Disparity": disparity.round(2),
        "MinDisp10": min_disp.round(2),
        "AvgTurnoverUSD": turnover,
    })[hit]
    out["PassFilter"] = (out["Close"] >= MIN_PRICE_USD) & (out["AvgTurnoverUSD"] >= min_turnover_usd)
    return out


# ════════════════════════════════════════════════════════════
# 3. 데이터 다운로드
# ════════════════════════════════════════════════════════════
def get_usdkrw() -> float:
    try:
        h = yf.Ticker("KRW=X").history(period="5d")
        rate = float(h["Close"].dropna().iloc[-1])
        if 500 < rate < 3000:
            return rate
    except Exception as e:
        print(f"[경고] 환율 조회 실패: {e}")
    print(f"[경고] 기본 환율 {FALLBACK_USDKRW} 사용")
    return FALLBACK_USDKRW


def download_batch(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    """여러 종목을 한 번에 받아 {ticker: OHLCV DataFrame} 형태로 반환."""
    for attempt in range(3):
        try:
            data = yf.download(
                tickers,
                period=period,
                interval="1d",
                auto_adjust=False,   # TradingView 기본 차트와 동일(분할만 반영, 배당 미반영)
                actions=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )
            break
        except Exception as e:
            print(f"  다운로드 오류(재시도 {attempt + 1}/3): {e}")
            time.sleep(5 * (attempt + 1))
    else:
        return {}

    result = {}
    if data is None or data.empty:
        return result
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                sub = data[t].dropna(how="all")
                if not sub.empty:
                    result[t] = sub
    elif len(tickers) == 1:
        result[tickers[0]] = data
    return result


def drop_incomplete_bar(df: pd.DataFrame) -> pd.DataFrame:
    """미국 장 마감(16:00 ET) 전이면 오늘 진행 중인 봉을 제외 (Pine barstate.isconfirmed 대응)."""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="America/New_York")
    if df.index[-1].date() == now.date() and (now.hour, now.minute) < (16, 0):
        return df.iloc[:-1]
    return df


# ════════════════════════════════════════════════════════════
# 4. 실행 모드
# ════════════════════════════════════════════════════════════
def run_codes(args, codes: list[str]) -> tuple[str, bool]:
    """지정 종목의 과거 시그널 일자 목록."""
    period = args.period or "max"
    usdkrw = get_usdkrw()
    min_turnover_usd = MIN_TURNOVER_KRW / usdkrw

    print(f"▶ 종목 조회: {', '.join(codes)} (기간 {period})")
    frames = download_batch(codes, period)

    lines = [f"🔍 MACD GC + 이격도 과매도 시그널 조회",
             f"종목: {', '.join(codes)} | 기간: {period}", ""]
    any_data = False
    for t in codes:
        df = frames.get(t)
        if df is None or df.empty:
            lines += [f"[{t}] 데이터 없음 (티커 확인 필요)", ""]
            print(f"  {t}: 데이터 없음")
            continue
        any_data = True
        if not args.include_today:
            df = drop_incomplete_bar(df)
        sig = find_signal_dates(df, min_turnover_usd)
        start, end = df.index[0].date(), df.index[-1].date()
        if sig.empty:
            lines += [f"[{t}] 시그널 없음 ({start} ~ {end})", ""]
            print(f"  {t}: 시그널 없음")
            continue

        shown = sig.tail(args.max_signals).iloc[::-1] if args.max_signals > 0 else sig.iloc[::-1]
        lines.append(f"[{t}] 시그널 {len(sig)}회 ({start} ~ {end})"
                     + (f", 최근 {len(shown)}개" if len(shown) < len(sig) else ""))
        for d, r in shown.iterrows():
            mark = "" if r["PassFilter"] else "  (1·2차 조건 미충족)"
            lines.append(f"• {d.date()}  ${r['Close']:,.2f}{mark}")
        lines.append("")

        print(f"\n[{t}] 시그널 {len(sig)}회")
        print(shown.drop(columns="AvgTurnoverUSD").to_string())

    return "\n".join(lines).strip(), any_data, []


def run_scan(args) -> tuple[str, bool]:
    """전체 종목 스캔 → 오늘 시그널."""
    period = args.period or "1y"
    tickers = list(dict.fromkeys(to_yf_ticker(t) for t in TICKERS))   # 순서 유지 중복 제거
    if args.limit:
        tickers = tickers[:args.limit]
    print(f"▶ TICKERS 리스트 {len(tickers):,}종목 스캔")

    usdkrw = get_usdkrw()
    min_turnover_usd = MIN_TURNOVER_KRW / usdkrw
    print(f"▶ 환율 USD/KRW = {usdkrw:,.2f} → 300억 원 ≈ ${min_turnover_usd / 1e6:,.1f}M")

    rows = []
    n_batches = (len(tickers) + args.batch_size - 1) // args.batch_size

    for b in range(n_batches):
        batch = tickers[b * args.batch_size:(b + 1) * args.batch_size]
        print(f"▶ 다운로드 {b + 1}/{n_batches} ({len(batch)}종목)", end="\r", flush=True)
        frames = download_batch(batch, period)
        for t, df in frames.items():
            if not args.include_today:
                df = drop_incomplete_bar(df)
            try:
                res = evaluate(df, min_turnover_usd)
            except Exception:
                res = None
            if res:
                res["Ticker"] = t
                rows.append(res)
        time.sleep(1)  # 과도한 요청 방지
    print()

    if not rows:
        return (f"⚠️ 스캔 실패: 시세 데이터를 받지 못했습니다. (대상 {len(tickers):,}종목)", False, [])

    res = pd.DataFrame(rows)
    # 거래정지·상폐 등으로 최신 봉이 없는 종목 제외 → 가장 최근 거래일 기준
    scan_date = res["Date"].max()
    res = res[res["Date"] == scan_date]
    res["AvgTurnoverKRW_억"] = (res["AvgTurnoverUSD"] * usdkrw / 1e8).round(0)
    res["AvgTurnoverUSD"] = res["AvgTurnoverUSD"].round(0)

    passed = res[res["Pass1_Price"] & res["Pass2_Turnover"]]
    hits = passed[passed["GoldenCross"] & passed["WasOversold"]]
    hits = hits.sort_values("AvgTurnoverUSD", ascending=False).copy()
    hits["Name"] = [get_company_name(t) for t in hits["Ticker"]]   # 시그널 종목만 이름 조회

    print(f"  기준일 {scan_date} | 데이터 {len(res):,}개 → 1·2차 통과 {len(passed):,}개 → 시그널 {len(hits):,}개")

    cols = ["Ticker", "Date", "Close", "Disparity", "MinDisp10",
            "MACD", "Signal", "AvgTurnoverKRW_억", "Name"]
    if hits.empty:
        print("\n오늘 조건을 만족하는 종목이 없습니다.")
    else:
        with pd.option_context("display.max_rows", None, "display.width", 200,
                               "display.max_colwidth", 40):
            print("\n" + hits[cols].to_string(index=False))

    out = args.out or f"scan_{pd.Timestamp.now():%Y%m%d}.csv"
    hits[cols].to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n결과 저장: {out}")

    header = [f"📈 MACD GC + 이격도 과매도 스캔 ({scan_date})",
              f"대상 {len(res):,}종목 → 1·2차 통과 {len(passed):,} → 시그널 {len(hits):,}", ""]
    if hits.empty:
        body = ["📭 오늘은 시그널 없음"]
    else:
        body = [f"{i}. {r.Ticker}" + (f" ({r.Name})" if r.Name else "")
                + f" | {r.Date} | ${r.Close:,.2f}"
                for i, r in enumerate(hits.itertuples(index=False), 1)]
        body += ["", "👇 종목별 메시지에서 승인/패스를 선택하세요."]

    # 종목별 시그널 메시지 + 승인/패스 버튼
    signal_msgs = []
    for r in hits.itertuples(index=False):
        text = "\n".join([
            f"📈 시그널: {r.Ticker}" + (f" ({r.Name})" if r.Name else ""),
            f"날짜: {r.Date}",
            f"종가: ${r.Close:,.2f}",
            f"이격도: {r.Disparity:.2f} (직전 10봉 최저 {r.MinDisp10:.2f})",
            f"20일 평균 거래대금: 약 {r.AvgTurnoverKRW_억:,.0f}억 원",
        ])
        signal_msgs.append((text, signal_keyboard(r.Ticker)))
    return "\n".join(header + body), True, signal_msgs


# ════════════════════════════════════════════════════════════
# 5. 메인
# ════════════════════════════════════════════════════════════
def parse_codes(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    codes = [to_yf_ticker(c) for item in raw for c in item.split(",") if c.strip()]
    return list(dict.fromkeys(codes))   # 순서 유지 중복 제거


def main() -> None:
    ap = argparse.ArgumentParser(description="MACD 골든크로스 + 이격도 과매도 스캐너")
    ap.add_argument("--codes", nargs="+", help="조회할 종목 (예: --codes TSLA AAPL 또는 TSLA,AAPL)")
    ap.add_argument("--max-signals", type=int, default=10,
                    help="--codes 조회 시 종목별로 보여줄 최근 시그널 수 (0 = 전부, 기본 10)")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 N종목만 스캔 (테스트용)")
    ap.add_argument("--batch-size", type=int, default=100, help="yfinance 한 번에 받을 종목 수")
    ap.add_argument("--period", default=None,
                    help="다운로드 기간 (기본: 전체 스캔 1y, --codes 조회 max)")
    ap.add_argument("--include-today", action="store_true", help="장중 미완성 봉도 포함")
    ap.add_argument("--out", default="", help="결과 CSV 경로 (기본: scan_YYYYMMDD.csv)")
    args = ap.parse_args()

    codes = parse_codes(args.codes)
    try:
        message, ok, signal_msgs = run_codes(args, codes) if codes else run_scan(args)
    except Exception as e:
        mode = f"종목 조회({', '.join(codes)})" if codes else "전체 스캔"
        send_telegram(f"❌ 스캐너 실행 오류 - {mode}\n{type(e).__name__}: {e}")
        raise

    send_telegram(message)
    for text, keyboard in signal_msgs:
        time.sleep(0.5)                     # 텔레그램 전송 속도 제한 여유
        send_telegram(text, reply_markup=keyboard)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
