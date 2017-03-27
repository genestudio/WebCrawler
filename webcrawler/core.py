import os
import time
import queue
import re
import threading
import copy
from collections import OrderedDict
import requests
import lxml.html
from lxml.cssselect import CSSSelector

try:
    # Python3
    from urllib.parse import urlparse
except ImportError:
    # Python2
    from urlparse import urlparse

from .helpers import color_logging
from .url_queue import UrlQueue
from . import helpers


def parse_seeds(seeds):
    """ parse website seeds.
    @params
        seeds example:
            - url1
            - user1:pwd1@url1
            - user1:pwd1@url1|url2|user3:pwd3@url3
    """
    seeds = seeds.strip().split('|')
    website_list = []
    for seed in seeds:
        if '@' not in seed:
            website = {
                'url': seed,
                'auth': None
            }
        else:
            user_pwd, url = seed.split('@')
            username, password = user_pwd.split(':')
            website = {
                'url': url,
                'auth': (username, password)
            }

        website_list.append(website)

    return website_list

class WebCrawler(object):

    def __init__(self, seeds):
        self.website_list = parse_seeds(seeds)
        self.reset_all()
        self.load_config()

    def reset_all(self):
        self.current_depth = 0
        self.test_counter = 0
        self.categorised_urls = {}
        self.web_urls_mapping = {}
        self.bad_urls_mapping = {}
        self.internal_hosts_list = []
        self.auth_dict = {}
        self.url_queue = UrlQueue()
        self.urlparsed_object_mapping = {}

        for website in self.website_list:
            website_url = website['url']
            self.url_queue.add_unvisited_urls(website_url)
            host = self.get_parsed_object_from_url(website_url).netloc
            self.internal_hosts_list.append(host)
            if website['auth']:
                self.auth_dict[host] = website['auth']

    def load_config(self):
        self.kwargs = {
            'headers': {}
        }
        config_file = os.path.join(os.path.dirname(__file__), 'config.yml')
        config_dict = helpers.load_yaml_file(config_file)
        self.url_type_config = config_dict['Content-Type']
        headers = config_dict['headers']
        self.user_agent = headers['User-Agent']
        self.kwargs['timeout'] = config_dict['default_timeout']

    def get_parsed_object_from_url(self, url):
        if url in self.urlparsed_object_mapping:
            return self.urlparsed_object_mapping[url]

        parsed_object = urlparse(url)
        self.urlparsed_object_mapping[url] = parsed_object
        return parsed_object

    def get_user_agent_by_url(self, url):
        if '//m.' in url:
            # e.g. http://m.debugtalk.com
            return self.user_agent['mobile']
        else:
            return self.user_agent['www']

    def parse_url(self, url, referer_url):

        def _make_url_by_referer(origin_parsed_obj, referer_url):
            """
            @params
                referer_url: e.g. https://store.debugtalk.com/product/osmo
                origin_parsed_obj.path e.g.:
                    (1) complete urls: http(s)://store.debugtalk.com/product/phantom-4-pro
                    (2) cdn asset files: //asset1.xcdn.com/assets/xxx.png
                    (3) relative links type1: /category/phantom
                    (4) relative links type2: mavic-pro
                    (5) relative links type3: ../compare-phantom-3
            @return
                corresponding result url:
                    (1) http(s)://store.debugtalk.com/product/phantom-4-pro
                    (2) http://asset1.xcdn.com/assets/xxx.png
                    (3) https://store.debugtalk.com/category/phantom
                    (4) https://store.debugtalk.com/product/mavic-pro
                    (5) https://store.debugtalk.com/compare-phantom-3
            """
            if origin_parsed_obj.scheme != "":
                # complete urls, e.g. http(s)://store.debugtalk.com/product/phantom-4-pro
                return origin_parsed_obj

            elif origin_parsed_obj.netloc != "":
                # cdn asset files, e.g. //asset1.xcdn.com/assets/xxx.png
                origin_parsed_obj = origin_parsed_obj._replace(scheme='http')
                return origin_parsed_obj

            elif origin_parsed_obj.path.startswith('/'):
                # relative links, e.g. /category/phantom
                referer_url_parsed_object = self.get_parsed_object_from_url(referer_url)
                origin_parsed_obj = origin_parsed_obj._replace(
                    scheme=referer_url_parsed_object.scheme,
                    netloc=referer_url_parsed_object.netloc
                )
                return origin_parsed_obj
            else:
                referer_url_parsed_object = self.get_parsed_object_from_url(referer_url)
                path_list = referer_url_parsed_object.path.split('/')

                if origin_parsed_obj.path.startswith('../'):
                    # relative links, e.g. ../compare-phantom-3
                    path_list.pop()
                    path_list[-1] = origin_parsed_obj.path.lstrip('../')
                else:
                    # relative links, e.g. mavic-pro
                    path_list[-1] = origin_parsed_obj.path

                new_path = '/'.join(path_list)
                origin_parsed_obj = origin_parsed_obj._replace(path=new_path)

                origin_parsed_obj = origin_parsed_obj._replace(
                    scheme=referer_url_parsed_object.scheme,
                    netloc=referer_url_parsed_object.netloc
                )
                return origin_parsed_obj

        url = url.replace(' ', '')
        if url == "":
            return None

        if url.startswith('#'):
            # locator, e.g. #overview
            return None

        if url.startswith('javascript'):
            # javascript:void(0);
            return None

        if url.startswith('mailto'):
            # mailto:buyenterprise@debugtalk.com
            return None

        if url.startswith('tel:'):
            # tel:00852+12345678
            return None

        if url.startswith('\\"'):
            # \\"https:\\/\\/store.debugtalk.com\\/guides\\/"
            url = url.encode('utf-8').decode('unicode_escape')\
                .replace(r'\/', r'/').replace(r'"', r'')
            return url

        if url.startswith('data:'):
            # data:image/png;base64,iVBORw
            return None

        if '@!' in url:
            # https://skypixel.aliyuncs.com/uploads/f23113e01.jpeg@!1200
            # remove picture size in url
            url = re.sub(r"@!.*$", "", url)

        parsed_object = self.get_parsed_object_from_url(url)

        # remove url fragment
        parsed_object = parsed_object._replace(fragment='')
        parsed_object = _make_url_by_referer(parsed_object, referer_url)

        return parsed_object.geturl()

    def get_url_type(self, resp, req_host):
        try:
            content_type = resp.headers['Content-Type']
        except KeyError:
            url_type = 'IGNORE'
            return url_type

        if content_type in self.url_type_config['static']:
            url_type = 'static'
        elif req_host not in self.internal_hosts_list:
            url_type = 'external'
        else:
            url_type = 'recursive'
        return url_type

    def parse_urls(self, urls_set, referer_url):
        parsed_urls_set = set()
        for url in urls_set:
            parsed_url = self.parse_url(url, referer_url)
            if parsed_url is None:
                continue
            parsed_urls_set.add(parsed_url)
        return parsed_urls_set

    def parse_page_links(self, referer_url, content):
        """ parse a web pages and get all hyper links.
        """
        raw_links_set = set()

        try:
            tree = lxml.html.fromstring(content)
        except lxml.etree.ParserError:
            return raw_links_set

        select_a = CSSSelector('a')
        select_link = CSSSelector('link')
        select_script = CSSSelector('script')
        select_img = CSSSelector('img')
        link_elements_list = select_a(tree) + select_link(tree) \
            + select_script(tree) + select_img(tree)
        for link in link_elements_list:
            url = link.get('href') or link.get('src')
            if url is None:
                continue
            raw_links_set.add(url)

        parsed_urls_set = self.parse_urls(raw_links_set, referer_url)
        return parsed_urls_set

    def save_categorised_url(self, status_code, url):
        """ save url by status_code category
        """
        if status_code not in self.categorised_urls:
            self.categorised_urls[status_code] = set()

        self.categorised_urls[status_code].add(url)

    def _print_log(self, depth, url, status_code, duration_time):
        self.test_counter += 1
        color_logging(
            "test_counter: {}, depth: {}, url: {}, status_code: {}, duration_time: {}s"
            .format(self.test_counter, depth, url, status_code, round(duration_time, 3)), 'DEBUG')

    def get_hyper_links(self, url, depth, retry_times=3):
        hyper_links_set = set()
        kwargs = copy.deepcopy(self.kwargs)
        kwargs['headers']['User-Agent'] = self.get_user_agent_by_url(url)
        parsed_object = self.get_parsed_object_from_url(url)
        url_host = parsed_object.netloc
        if url_host in self.auth_dict and self.auth_dict[url_host]:
            kwargs['auth'] = self.auth_dict[url_host]

        exception_str = ""
        status_code = '0'
        resp_md5 = None
        duration_time = 0
        try:
            start_time = time.time()
            resp = requests.head(url, **kwargs)
            url_type = self.get_url_type(resp, url_host)
            if url_type in ['static', 'external']:
                duration_time = time.time() - start_time
                status_code = str(resp.status_code)
            elif url_type == 'IGNORE':
                duration_time = time.time() - start_time
                status_code = str(resp.status_code)
                retry_times = 0
            else:
                # recursive
                start_time = time.time()
                resp = requests.get(url, **kwargs)
                duration_time = time.time() - start_time
                resp_md5 = helpers.get_md5(resp.content)
                hyper_links_set = self.parse_page_links(resp.url, resp.content)
                if url not in self.web_urls_mapping:
                    self.web_urls_mapping[url] = hyper_links_set
                status_code = str(resp.status_code)
                self.url_queue.add_unvisited_urls(hyper_links_set)
                if resp.status_code > 400:
                    exception_str = 'HTTP Status Code is {}.'.format(status_code)
        except requests.exceptions.SSLError as ex:
            color_logging("{}: {}".format(url, str(ex)), 'WARNING')
            exception_str = str(ex)
            status_code = 'SSLError'
            retry_times = 0
        except requests.exceptions.ConnectionError as ex:
            color_logging("ConnectionError {}: {}".format(url, str(ex)), 'WARNING')
            exception_str = str(ex)
            status_code = 'ConnectionError'
        except requests.exceptions.Timeout:
            time_out = kwargs['timeout']
            color_logging("Timeout {}: Timed out for {} seconds".format(url, time_out), 'WARNING')
            exception_str = "Timed out for {} seconds".format(time_out)
            status_code = 'Timeout'
        except requests.exceptions.InvalidSchema as ex:
            color_logging("{}: {}".format(url, str(ex)), 'WARNING')
            exception_str = str(ex)
            status_code = 'InvalidSchema'
            retry_times = 0
        except requests.exceptions.ChunkedEncodingError as ex:
            color_logging("{}: {}".format(url, str(ex)), 'WARNING')
            exception_str = str(ex)
            status_code = 'ChunkedEncodingError'

        self._print_log(depth, url, status_code, duration_time)
        if retry_times > 0:
            if not status_code.isdigit() or int(status_code) > 400:
                time.sleep((4-retry_times)*2)
                return self.get_hyper_links(url, depth, retry_times-1)
        else:
            self.bad_urls_mapping[url] = exception_str

        self.save_categorised_url(status_code, url)
        self.url_queue.add_visited_url(url, resp_md5)
        return hyper_links_set

    def get_referer_urls_set(self, url):
        """ get all referer urls of the specified url.
        """
        referer_set = set()
        for parent_url, url_list in self.web_urls_mapping.items():
            if url in url_list:
                referer_set.add(parent_url)
        return referer_set

    def print_categorised_urls(self, categorised_urls):

        def _print(status_code, urls_list, log_level, show_referer=False):
            if isinstance(status_code, str):
                output = "{}: {}.\n".format(status_code, len(urls_list))
            elif isinstance(status_code, int):
                output = "HTTP status code {}, total: {}.\n".format(status_code, len(urls_list))

            output += "urls list: \n"
            for url in urls_list:
                output += url
                if not str(status_code).isdigit():
                    output += ", {}: {}".format(status_code, self.bad_urls_mapping[url])
                if show_referer:
                    # only show 5 referers if referer urls number is greater than 5
                    referer_urls = self.get_referer_urls_set(url)
                    referer_urls_num = len(referer_urls)
                    if referer_urls_num > 5:
                        referer_urls = list(referer_urls)[:5]
                        output += ", referer_urls: {}".format(referer_urls)
                        output += " total {}, displayed 5.".format(referer_urls_num)
                    else:
                        output += ", referer_urls: {}".format(referer_urls)
                output += '\n'

            color_logging(output, log_level)

        self.categorised_urls_sorted_items = OrderedDict(
            sorted(categorised_urls.items(), reverse=True)
        ).items()

        for status_code, urls_list in self.categorised_urls_sorted_items:
            color_logging('-' * 120)
            if status_code.isdigit():
                status_code = int(status_code)
                if status_code >= 500:
                    _print(status_code, urls_list, 'ERROR', True)
                elif status_code >= 400:
                    _print(status_code, urls_list, 'ERROR', True)
                elif status_code >= 300:
                    _print(status_code, urls_list, 'WARNING')
                elif status_code > 200:
                    _print(status_code, urls_list, 'INFO')
            else:
                _print(status_code, urls_list, 'ERROR', True)

    def run_dfs(self, max_depth):
        """ start to run test in DFS mode.
        """
        def crawler(url, depth):
            """ DFS crawler
            """
            if depth > max_depth:
                return

            if self.url_queue.is_url_visited(url):
                urls = set()
            else:
                urls = self.get_hyper_links(url, depth)

            for url in urls:
                crawler(url, depth+1)

        while not self.url_queue.is_unvisited_urls_empty():
            url = self.url_queue.get_one_unvisited_url()
            crawler(url, self.current_depth)

    def run_bfs(self, max_depth, max_concurrent_workers):
        """ start to run test in BFS mode.
        """
        while self.current_depth <= max_depth:
            self.concurrent_visit_all_unvisited_urls(max_concurrent_workers)
            self.current_depth += 1

    def concurrent_visit_all_unvisited_urls(self, max_concurrent_workers):

        current_depth_unvisited_urls_queue = queue.Queue()

        while True:
            url = self.url_queue.get_one_unvisited_url()
            if url is None:
                break
            current_depth_unvisited_urls_queue.put_nowait(url)

        q_size = current_depth_unvisited_urls_queue.qsize()
        if q_size < max_concurrent_workers:
            worker_threads_num = q_size
        else:
            worker_threads_num = max_concurrent_workers

        threads = []
        for _ in range(worker_threads_num):
            thread = threading.Thread(
                target=self.visit_url,
                args=(current_depth_unvisited_urls_queue, self.current_depth,)
            )
            thread.daemon = False
            thread.start()
            threads.append(thread)

        current_depth_unvisited_urls_queue.join()

        for thread in threads:
            thread.join()

    def visit_url(self, unvisited_urls_queue, depth):
        while True:
            try:
                url = unvisited_urls_queue.get(block=True, timeout=5)
            except queue.Empty:
                break

            self.get_hyper_links(url, depth)
            unvisited_urls_queue.task_done()

    def start(self, crawl_mode='BFS', max_depth=10, max_concurrent_workers=20):
        """ start to run test in specified crawl_mode.
        @params
            crawl_mode = 'BFS' or 'DFS'
        """
        info = "Start to run test in {} mode, max_depts: {}"\
            .format(crawl_mode, max_depth)
        color_logging(info)

        if crawl_mode.upper() == 'BFS':
            self.run_bfs(max_depth, max_concurrent_workers)
        else:
            self.run_dfs(max_depth)

        color_logging("Finished. The crawler has tested {} urls."\
            .format(self.url_queue.get_visited_urls_count()))
        self.print_categorised_urls(self.categorised_urls)

    def save_visited_urls(self, yaml_log_path):
        helpers.save_to_yaml(self.url_queue.get_visited_urls(), yaml_log_path)
        color_logging("Save visited urls in YAML file: {}".format(yaml_log_path))

    def gen_mail_content(self, jenkins_log_url):
        website_urls = [website['url'] for website in self.website_list]
        content = "Tested websites: {}<br/>".format(','.join(website_urls))
        content += "Total tested urls number: {}<br/><br/>"\
            .format(self.url_queue.get_visited_urls_count())
        content += "Categorised urls number by HTTP Status Code: <br/>"
        for status_code, urls_list in self.categorised_urls_sorted_items:
            if status_code.isdigit():
                content += "status code {}: {}".format(status_code, len(urls_list))
            else:
                content += "{} urls: {}".format(status_code, len(urls_list))

            content += "<br/>"

        content += "<br/>Detailed Jenkins log info: {}".format(jenkins_log_url)
        mail_content = {
            'type': 'html',
            'content': content
        }
        return mail_content