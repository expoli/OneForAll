#!/usr/bin/python3
# coding=utf-8

"""
OneForAll subdomain brute module

:copyright: Copyright (c) 2019, Jing Ling. All rights reserved.
:license: GNU General Public License v3.0, see LICENSE for more details.
"""
import gc
import json
import time
import secrets

import exrex
import fire
import tenacity
from dns.exception import Timeout
from dns.resolver import NXDOMAIN, YXDOMAIN, NoAnswer, NoNameservers

import dbexport
from common import utils
from common import similarity
from config import settings
from common.module import Module
from config.log import logger


def config_resolver(nameservers):
    """
    配置DNS解析器

    :param nameservers: 名称解析服务器地址
    """
    resolver = utils.dns_resolver()
    resolver.nameservers = nameservers
    resolver.rotate = True  # 随机使用NS
    resolver.cache = None  # 不使用DNS缓存
    return resolver


def gen_random_subdomains(domain, count):
    """
    生成指定数量的随机子域域名列表

    :param domain: 主域
    :param count: 数量
    """
    subdomains = set()
    if count < 1:
        return subdomains
    for _ in range(count):
        token = secrets.token_hex(4)
        subdomains.add(f'{token}.{domain}')
    return subdomains


def query_a_record(subdomain, resolver):
    """
    查询子域A记录

    :param subdomain: 子域
    :param resolver: DNS解析器
    """
    try:
        answer = resolver.query(subdomain, 'A')
    except Exception as e:
        logger.log('DEBUG', f'Query {subdomain} wildcard dns record error')
        logger.log('DEBUG', e.args)
        return False
    if answer.rrset is None:
        return False
    ttl = answer.ttl
    name = answer.name
    ips = {item.address for item in answer}
    logger.log('ALERT', f'{subdomain} resolve to: {name} '
                        f'IP: {ips} TTL: {ttl}')
    return True


def all_resolve_success(subdomains):
    """
    判断是否所有子域都解析成功

    :param subdomains: 子域列表
    """
    resolver = utils.dns_resolver()
    resolver.cache = None  # 不使用DNS缓存
    status = set()
    for subdomain in subdomains:
        status.add(query_a_record(subdomain, resolver))
    return all(status)


def all_request_success(subdomains):
    """
    判断是否所有子域都请求成功

    :param subdomains: 子域列表
    """
    result = list()
    for subdomain in subdomains:
        url = f'http://{subdomain}'
        resp = utils.get_url_resp(url)
        if resp:
            logger.log('ALERT', f'Request: {url} Status: {resp.status_code} '
                                f'Size: {len(resp.content)}')
            result.append(resp.text)
        else:
            result.append(resp)
    return all(result), result


def any_similar_html(resp_list):
    """
    判断是否有一组HTML页面结构相似

    :param resp_list: 响应HTML页面
    """
    html_doc1, html_doc2, html_doc3 = resp_list
    if similarity.is_similar(html_doc1, html_doc2):
        return True
    if similarity.is_similar(html_doc1, html_doc3):
        return True
    if similarity.is_similar(html_doc2, html_doc3):
        return True
    return False


def detect_wildcard(domain):
    """
    Detect use wildcard dns record or not

    :param  str  domain:  domain
    :return bool use wildcard dns record or not
    """
    logger.log('INFOR', f'Detecting {domain} use wildcard dns record or not')
    random_subdomains = gen_random_subdomains(domain, 3)
    if not all_resolve_success(random_subdomains):
        return False
    is_all_success, all_request_resp = all_request_success(random_subdomains)
    if not is_all_success:
        return True
    return any_similar_html(all_request_resp)


def is_enable_wildcard(domain):
    is_enable = detect_wildcard(domain)
    if is_enable:
        logger.log('ALERT', f'The domain {domain} enables wildcard')
    else:
        logger.log('ALERT', f'The domain {domain} disables wildcard')
    return is_enable


def gen_subdomains(expression, path):
    """
    Generate subdomains

    :param  str  expression: generate subdomains's expression
    :param  str  path: path of wordlist
    :return set  subdomains: list of subdomains
    """
    subdomains = set()
    with open(path, encoding='utf-8', errors='ignore') as fd:
        for line in fd:
            word = line.strip().lower()
            if len(word) == 0:
                continue
            if not utils.is_subname(word):
                continue
            if word.startswith('.'):
                word = word[1:]
            if word.endswith('.'):
                word = word[:-1]
            subdomain = expression.replace('*', word)
            subdomains.add(subdomain)
    size = len(subdomains)
    logger.log('DEBUG', f'The size of the dictionary generated by {path} is {size}')
    if size == 0:
        logger.log('ALERT', 'Please check the dictionary content!')
    else:
        utils.check_random_subdomain(subdomains)
    return subdomains


def gen_fuzz_subdomains(expression, rule, fuzzlist):
    """
    Generate subdomains based on fuzz mode

    :param  str  expression: generate subdomains's expression
    :param  str  rule: regexp rule
    :param  str  fuzzlist: fuzz dictionary
    :return set  subdomains: list of subdomains
    """
    subdomains = set()
    if fuzzlist:
        fuzz_domain = gen_subdomains(expression, fuzzlist)
        subdomains.update(fuzz_domain)
    if rule:
        fuzz_count = exrex.count(rule)
        if fuzz_count > 10000000:
            logger.log('ALERT', f'The dictionary generated by this rule is too large: '
                                f'{fuzz_count} > 10000000')
        for fuzz_string in exrex.generate(rule):
            fuzz_string = fuzz_string.lower()
            if not fuzz_string.isalnum():
                continue
            fuzz_domain = expression.replace('*', fuzz_string)
            subdomains.add(fuzz_domain)
        utils.check_random_subdomain(subdomains)
    logger.log('DEBUG', f'Dictionary size based on fuzz mode: {len(subdomains)}')
    return subdomains


def gen_word_subdomains(expression, path):
    """
    Generate subdomains based on word mode

    :param  str  expression: generate subdomains's expression
    :param  str  path: path of wordlist
    :return set  subdomains: list of subdomains
    """
    subdomains = gen_subdomains(expression, path)
    logger.log('DEBUG', f'Dictionary based on word mode size: {len(subdomains)}')
    return subdomains


def query_domain_ns_a(ns_list):
    logger.log('INFOR', f'Querying A record from authoritative name server: {ns_list} ')
    if not isinstance(ns_list, list):
        return list()
    ns_ip_list = []
    resolver = utils.dns_resolver()
    for ns in ns_list:
        try:
            answer = resolver.query(ns, 'A')
        except Exception as e:
            logger.log('ERROR', e.args)
            logger.log('ERROR', f'Query authoritative name server {ns} A record error')
            continue
        if answer:
            for item in answer:
                ns_ip_list.append(item.address)
    logger.log('INFOR', f'Authoritative name server A record result: {ns_ip_list}')
    return ns_ip_list


def query_domain_ns(domain):
    logger.log('INFOR', f'Querying NS records of {domain}')
    domain = utils.get_main_domain(domain)
    resolver = utils.dns_resolver()
    try:
        answer = resolver.query(domain, 'NS')
    except Exception as e:
        logger.log('ERROR', e.args)
        logger.log('ERROR', f'Querying NS records of {domain} error')
        return list()
    ns = [item.to_text() for item in answer]
    logger.log('INFOR', f'{domain}\'s authoritative name server is {ns}')
    return ns


@tenacity.retry(stop=tenacity.stop_after_attempt(2))
def get_wildcard_record(domain, resolver):
    logger.log('INFOR', f"Query {domain} 's wildcard dns record "
                        f"in authoritative name server")
    try:
        answer = resolver.query(domain, 'A')
    # 如果查询随机域名A记录时抛出Timeout异常则重新查询
    except Timeout as e:
        logger.log('ALERT', f'Query timeout, retrying')
        logger.log('DEBUG', e.args)
        raise tenacity.TryAgain
    except (NXDOMAIN, YXDOMAIN, NoAnswer, NoNameservers) as e:
        logger.log('DEBUG', e.args)
        logger.log('DEBUG', f'{domain} dont have A record on authoritative name server')
        return None, None
    except Exception as e:
        logger.log('ERROR', e.args)
        logger.log('ERROR', f'Query {domain} wildcard dns record in '
                            f'authoritative name server error')
        exit(1)
    else:
        if answer.rrset is None:
            logger.log('DEBUG', f'No record of query result')
            return None, None
        name = answer.name
        ip = {item.address for item in answer}
        ttl = answer.ttl
        logger.log('INFOR', f'{domain} results on authoritative name server: {name} '
                            f'IP: {ip} TTL: {ttl}')
        return ip, ttl


def collect_wildcard_record(domain, authoritative_ns):
    logger.log('INFOR', f'Collecting wildcard dns record for {domain}')
    if not authoritative_ns:
        return list(), int()
    resolver = utils.dns_resolver()
    resolver.nameservers = authoritative_ns  # 使用权威名称服务器
    resolver.rotate = True  # 随机使用NS
    resolver.cache = None  # 不使用DNS缓存
    ips = set()
    ttl = int()
    ttls_check = list()
    ips_stat = dict()
    ips_check = list()
    while True:
        token = secrets.token_hex(4)
        random_subdomain = f'{token}.{domain}'
        try:
            ip, ttl = get_wildcard_record(random_subdomain, resolver)
        except Exception as e:
            logger.log('DEBUG', e.args)
            logger.log('ALERT', f'Multiple query errors,'
                                f'try to query a new random subdomain')
            continue
        # 每5次查询检查结果列表 如果都没结果则结束查询
        ips_check.append(ip)
        ttls_check.append(ttl)
        if len(ips_check) == 5:
            if not any(ips_check):
                logger.log('ALERT', 'The query ends because there are '
                                    'no results for 5 consecutive queries.')
                break
            ips_check = list()
        if len(ttls_check) == 5 and len(set(ttls_check)) == 5:
            logger.log('ALERT', 'The query ends because there are '
                                '5 different TTL results for 5 consecutive queries.')
            ips, ttl = set(), int()
            break
        if ip is None:
            continue
        ips.update(ip)
        # 统计每个泛解析IP出现次数
        for addr in ip:
            count = ips_stat.setdefault(addr, 0)
            ips_stat[addr] = count + 1
        # 筛选出出现次数2次以上的IP地址
        addrs = list()
        for addr, times in ips_stat.items():
            if times >= 2:
                addrs.append(addr)
        # 大部分的IP地址出现次数大于2次停止收集泛解析IP记录
        if len(addrs) / len(ips) >= 0.8:
            break
    logger.log('DEBUG', f'Collected the wildcard dns record of {domain}\n{ips}\n{ttl}')
    return ips, ttl


def get_nameservers_path(enable_wildcard, ns_ip_list):
    path = settings.brute_nameservers_path
    if not enable_wildcard:
        return path
    if not ns_ip_list:
        return path
    path = settings.authoritative_dns_path
    ns_data = '\n'.join(ns_ip_list)
    utils.save_data(path, ns_data)
    return path


def check_dict():
    if not settings.enable_check_dict:
        return
    sec = settings.check_time
    logger.log('ALERT', f'You have {sec} seconds to check '
                        f'whether the configuration is correct or not')
    logger.log('ALERT', f'If you want to exit, please use `Ctrl + C`')
    try:
        time.sleep(sec)
    except KeyboardInterrupt:
        logger.log('INFOR', 'Due to configuration incorrect, exited')
        exit(0)


def gen_result_infos(items, infos, subdomains, ip_times, wc_ips, wc_ttl):
    qname = items.get('name')[:-1]  # 去除最右边的`.`点号
    reason = items.get('status')
    resolver = items.get('resolver')
    data = items.get('data')
    answers = data.get('answers')
    info = dict()
    cname = list()
    ips = list()
    public = list()
    times = list()
    ttls = list()
    is_valid_flags = list()
    have_a_record = False
    for answer in answers:
        if answer.get('type') != 'A':
            logger.log('TRACE', f'The query result of {qname} has no A record\n{answer}')
            continue
        logger.log('TRACE', f'The query result of {qname} no A record\n{answer}')
        have_a_record = True
        ttl = answer.get('ttl')
        ttls.append(ttl)
        cname.append(answer.get('name')[:-1])  # 去除最右边的`.`点号
        ip = answer.get('data')
        ips.append(ip)
        public.append(utils.ip_is_public(ip))
        num = ip_times.get(ip)
        times.append(num)
        isvalid, reason = is_valid_subdomain(ip, ttl, num, wc_ips, wc_ttl)
        logger.log('TRACE', f'{ip} effective: {isvalid} reason: {reason}')
        is_valid_flags.append(isvalid)
    if not have_a_record:
        logger.log('TRACE', f'All query result of {qname} no A record{answers}')
    # 为了优化内存 只添加有A记录且通过判断的子域到记录中
    if have_a_record and all(is_valid_flags):
        info['resolve'] = 1
        info['reason'] = reason
        info['ttl'] = ttls
        info['cname'] = cname
        info['ip'] = ips
        info['public'] = public
        info['times'] = times
        info['resolver'] = resolver
        infos[qname] = info
        subdomains.append(qname)
    return infos, subdomains


def stat_ip_times(result_paths):
    logger.log('INFOR', f'Counting IP')
    times = dict()
    for result_path in result_paths:
        logger.log('DEBUG', f'Reading {result_path}')
        with open(result_path) as fd:
            for line in fd:
                line = line.strip()
                try:
                    items = json.loads(line)
                except Exception as e:
                    logger.log('ERROR', e.args)
                    logger.log('ERROR', f'Error parsing {result_path} '
                                        f'line {line} Skip this line')
                    continue
                status = items.get('status')
                if status != 'NOERROR':
                    continue
                data = items.get('data')
                if 'answers' not in data:
                    continue
                answers = data.get('answers')
                for answer in answers:
                    if answer.get('type') == 'A':
                        ip = answer.get('data')
                        # 取值 如果是首次出现的IP集合 出现次数先赋值0
                        value = times.setdefault(ip, 0)
                        times[ip] = value + 1
    return times


def deal_output(output_paths, ip_times, wildcard_ips, wildcard_ttl):
    logger.log('INFOR', f'Processing result')
    infos = dict()  # 用来记录所有域名有关信息
    subdomains = list()  # 用来保存所有通过有效性检查的子域
    for output_path in output_paths:
        logger.log('DEBUG', f'Processing {output_path}')
        with open(output_path) as fd:
            for line in fd:
                line = line.strip()
                try:
                    items = json.loads(line)
                except Exception as e:
                    logger.log('ERROR', e.args)
                    logger.log('ERROR', f'Error parsing {line} Skip this line')
                    continue
                qname = items.get('name')[:-1]  # 去除最右边的`.`点号
                status = items.get('status')
                if status != 'NOERROR':
                    logger.log('TRACE', f'Found {qname}\'s result {status} '
                                        f'while processing {line}')
                    continue
                data = items.get('data')
                if 'answers' not in data:
                    logger.log('TRACE', f'Processing {line}, {qname} no response')
                    continue
                infos, subdomains = gen_result_infos(items, infos, subdomains,
                                                     ip_times, wildcard_ips,
                                                     wildcard_ttl)
    return infos, subdomains


def check_by_compare(ip, ttl, wc_ips, wc_ttl):
    """
    Use TTL comparison to detect wildcard dns record

    :param  set ip:     A record IP address set
    :param  int ttl:    A record TTL value
    :param  set wc_ips: wildcard dns record IP address set
    :param  int wc_ttl: wildcard dns record TTL value
    :return bool: result
    """
    # Reference：http://sh3ll.me/archives/201704041222.txt
    if ip not in wc_ips:
        return False  # 子域IP不在泛解析IP集合则不是泛解析
    if ttl != wc_ttl and ttl % 60 == 0 and wc_ttl % 60 == 0:
        return False
    return True


def check_ip_times(times):
    """
    Use IP address times to determine wildcard or not

    :param  times: IP address times
    :return bool:  result
    """
    if times > settings.ip_appear_maximum:
        return True
    return False


def is_valid_subdomain(ip, ttl, times, wc_ips, wc_ttl):
    ip_blacklist = settings.brute_ip_blacklist
    if ip in ip_blacklist:  # 解析ip在黑名单ip则为非法子域
        return 0, 'IP blacklist'
    if all([wc_ips, wc_ttl]):  # 有泛解析记录才进行对比
        if check_by_compare(ip, ttl, wc_ips, wc_ttl):
            return 0, 'IP wildcard'
    if check_ip_times(times):
        return 0, 'IP exceeded'
    return 1, 'OK'


def save_brute_dict(dict_path, dict_set):
    dict_data = '\n'.join(dict_set)
    if not utils.save_data(dict_path, dict_data):
        logger.log('FATAL', 'Saving dictionary error')
        exit(1)


def delete_file(dict_path, output_paths):
    if settings.delete_generated_dict:
        dict_path.unlink()
    if settings.delete_massdns_result:
        for output_path in output_paths:
            output_path.unlink()


class Brute(Module):
    """
    OneForAll subdomain brute module

    Example：
        brute.py --target domain.com --word True run
        brute.py --targets ./domains.txt --word True run
        brute.py --target domain.com --word True --concurrent 2000 run
        brute.py --target domain.com --word True --wordlist subnames.txt run
        brute.py --target domain.com --word True --recursive True --depth 2 run
        brute.py --target d.com --fuzz True --place m.*.d.com --rule '[a-z]' run
        brute.py --target d.com --fuzz True --place m.*.d.com --fuzzlist subnames.txt run

    Note:
        --format rst/csv/tsv/json/yaml/html/jira/xls/xlsx/dbf/latex/ods (result format)
        --path   Result path (default None, automatically generated)


    :param str  target:     One domain (target or targets must be provided)
    :param str  targets:    File path of one domain per line
    :param int  process:    Number of processes (default 1)
    :param int  concurrent: Number of concurrent (default 2000)
    :param bool word:       Use word mode generate dictionary (default False)
    :param str  wordlist:   Dictionary path used in word mode (default use ./config/default.py)
    :param bool recursive:  Use recursion (default False)
    :param int  depth:      Recursive depth (default 2)
    :param str  nextlist:   Dictionary file path used by recursive (default use ./config/default.py)
    :param bool fuzz:       Use fuzz mode generate dictionary (default False)
    :param bool alive:      Only export alive subdomains (default False)
    :param str  place:      Designated fuzz position (required if use fuzz mode)
    :param str  rule:       Specify the regexp rules used in fuzz mode (required if use fuzz mode)
    :param str  fuzzlist:   Dictionary path used in fuzz mode (default use ./config/default.py)
    :param bool export:     Export the results (default True)
    :param str  format:     Result format (default csv)
    :param str  path:       Result directory (default None)
    """
    def __init__(self, target=None, targets=None, process=None, concurrent=None,
                 word=False, wordlist=None, recursive=False, depth=None, nextlist=None,
                 fuzz=False, place=None, rule=None, fuzzlist=None, export=True,
                 alive=True, format='csv', path=None):
        Module.__init__(self)
        self.module = 'Brute'
        self.source = 'Brute'
        self.target = target
        self.targets = targets
        self.process_num = process or utils.get_process_num()
        self.concurrent_num = concurrent or settings.brute_concurrent_num
        self.word = word
        self.wordlist = wordlist or settings.brute_wordlist_path
        self.recursive_brute = recursive or settings.enable_recursive_brute
        self.recursive_depth = depth or settings.brute_recursive_depth
        self.recursive_nextlist = nextlist or settings.recursive_nextlist_path
        self.fuzz = fuzz or settings.enable_fuzz
        self.place = place or settings.fuzz_place
        self.rule = rule or settings.fuzz_rule
        self.fuzzlist = fuzzlist or settings.fuzz_list
        self.export = export
        self.alive = alive
        self.format = format
        self.path = path
        self.bulk = False  # 是否是批量爆破场景
        self.domains = list()  # 待爆破的所有域名集合
        self.domain = str()  # 当前正在进行爆破的域名
        self.ips_times = dict()  # IP集合出现次数
        self.enable_wildcard = False  # 当前域名是否使用泛解析
        self.check_env = True
        self.quite = False

    def gen_brute_dict(self, domain):
        logger.log('INFOR', f'Generating dictionary for {domain}')
        dict_set = set()
        # 如果domain不是self.subdomain 而是self.domain的子域则生成递归爆破字典
        if self.place is None:
            self.place = '*.' + domain
        wordlist = self.wordlist
        main_domain = utils.get_main_domain(domain)
        if domain != main_domain:
            wordlist = self.recursive_nextlist
        if self.word:
            word_subdomains = gen_word_subdomains(self.place, wordlist)
            dict_set.update(word_subdomains)
        if self.fuzz:
            fuzz_subdomains = gen_fuzz_subdomains(self.place, self.rule, self.fuzzlist)
            dict_set.update(fuzz_subdomains)
        count = len(dict_set)
        logger.log('INFOR', f'Dictionary size: {count}')
        if count > 10000000:
            logger.log('ALERT', f'The generated dictionary is '
                                f'too large {count} > 10000000')
        return dict_set

    def check_brute_params(self):
        if not (self.word or self.fuzz):
            logger.log('FATAL', f'Please specify at least one brute mode')
            exit(1)
        if len(self.domains) > 1:
            self.bulk = True
        if self.fuzz:
            if self.place is None:
                logger.log('FATAL', f'No fuzz position specified')
                exit(1)
            if self.rule is None and self.fuzzlist is None:
                logger.log('FATAL', f'No fuzz rules or fuzz dictionary specified')
                exit(1)
            if self.bulk:
                logger.log('FATAL', f'Cannot use fuzz mode in the bulk brute')
                exit(1)
            if self.recursive_brute:
                logger.log('FATAL', f'Cannot use recursive brute in fuzz mode')
                exit(1)
            fuzz_count = self.place.count('*')
            if fuzz_count < 1:
                logger.log('FATAL', f'No fuzz position specified')
                exit(1)
            if fuzz_count > 1:
                logger.log('FATAL', f'Only one fuzz position can be specified')
                exit(1)
            if self.domain not in self.place:
                logger.log('FATAL', f'Incorrect domain for fuzz')
                exit(1)

    def main(self, domain):
        start = time.time()
        logger.log('INFOR', f'Blasting {domain} ')
        massdns_dir = settings.third_party_dir.joinpath('massdns')
        result_dir = settings.result_save_dir
        temp_dir = result_dir.joinpath('temp')
        utils.check_dir(temp_dir)
        massdns_path = utils.get_massdns_path(massdns_dir)
        timestring = utils.get_timestring()

        wildcard_ips = list()  # 泛解析IP列表
        wildcard_ttl = int()  # 泛解析TTL整型值
        ns_list = query_domain_ns(self.domain)
        ns_ip_list = query_domain_ns_a(ns_list)  # DNS权威名称服务器对应A记录列表
        self.enable_wildcard = is_enable_wildcard(domain)

        if self.enable_wildcard:
            wildcard_ips, wildcard_ttl = collect_wildcard_record(domain,
                                                                 ns_ip_list)
        ns_path = get_nameservers_path(self.enable_wildcard, ns_ip_list)

        dict_set = self.gen_brute_dict(domain)

        dict_name = f'generated_subdomains_{domain}_{timestring}.txt'
        dict_path = temp_dir.joinpath(dict_name)
        save_brute_dict(dict_path, dict_set)
        del dict_set
        gc.collect()

        output_name = f'resolved_result_{domain}_{timestring}.json'
        output_path = temp_dir.joinpath(output_name)
        log_path = result_dir.joinpath('massdns.log')
        check_dict()
        logger.log('INFOR', f'Running massdns to brute subdomains')
        utils.call_massdns(massdns_path, dict_path, ns_path, output_path,
                           log_path, quiet_mode=self.quite,
                           process_num=self.process_num,
                           concurrent_num=self.concurrent_num)
        output_paths = []
        if self.process_num == 1:
            output_paths.append(output_path)
        else:
            for i in range(self.process_num):
                output_name = f'resolved_result_{domain}_{timestring}.json{i}'
                output_path = temp_dir.joinpath(output_name)
                output_paths.append(output_path)
        ip_times = stat_ip_times(output_paths)
        self.infos, self.subdomains = deal_output(output_paths, ip_times,
                                                  wildcard_ips, wildcard_ttl)
        delete_file(dict_path, output_paths)
        end = time.time()
        self.elapse = round(end - start, 1)
        logger.log('ALERT', f'{self.source} module takes {self.elapse} seconds, '
                            f'found {len(self.subdomains)} subdomains of {domain}')
        logger.log('DEBUG', f'{self.source} module found subdomains of {domain}: '
                            f'{self.subdomains}')
        self.gen_result()
        self.save_db()
        return self.subdomains

    def run(self):
        logger.log('INFOR', f'Start running {self.source} module')
        if self.check_env:
            utils.check_env()
        self.domains = utils.get_domains(self.target, self.targets)
        all_subdomains = list()
        for self.domain in self.domains:
            self.check_brute_params()
            if self.recursive_brute:
                logger.log('INFOR', f'Start recursively brute the 1 layer subdomain'
                                    f' of {self.domain}')
            valid_subdomains = self.main(self.domain)
            all_subdomains.extend(valid_subdomains)

            # 递归爆破下一层的子域
            # fuzz模式不使用递归爆破
            if self.recursive_brute:
                for layer_num in range(1, self.recursive_depth):
                    # 之前已经做过1层子域爆破 当前实际递归层数是layer+1
                    logger.log('INFOR', f'Start recursively brute the {layer_num + 1} layer'
                                        f' subdomain of {self.domain}')
                    for subdomain in all_subdomains:
                        self.place = '*.' + subdomain
                        # 进行下一层子域爆破的限制条件
                        num = subdomain.count('.') - self.domain.count('.')
                        if num == layer_num:
                            valid_subdomains = self.main(subdomain)
                            all_subdomains.extend(valid_subdomains)

            logger.log('INFOR', f'Finished {self.source} module\'s brute {self.domain}')
            if not self.path:
                name = f'{self.domain}_brute_result.{self.format}'
                self.path = settings.result_save_dir.joinpath(name)
            # 数据库导出
            if self.export:
                dbexport.export(self.domain,
                                type='table',
                                alive=self.alive,
                                limit='resolve',
                                path=self.path,
                                format=self.format)


if __name__ == '__main__':
    fire.Fire(Brute)
