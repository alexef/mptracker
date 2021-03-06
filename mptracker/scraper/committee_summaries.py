import re
from datetime import datetime
import subprocess
from urllib.parse import urlparse, parse_qs
from pyquery import PyQuery as pq
from flask import json
from path import path
from mptracker.scraper.common import (Scraper, pqitems, get_cached_session)


with (path(__file__).parent / 'committee_names.json').open('rb') as f:
    committee_names = json.loads(f.read().decode('utf-8'))


def pdf_to_text(pdf_bytes):
    """ run pdftotext from poppler """
    p = subprocess.Popen(['pdftotext', '-enc', 'UTF-8', '-', '-'],
                         stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE)
    outs, errs = p.communicate(pdf_bytes, timeout=10)
    return outs.decode('utf-8')


class SummaryScraper(Scraper):

    listing_page_url = ('http://www.cdep.ro/pls/proiecte/upl_com.lista'
                        '?nrc={offset}&an={year}&tip=1&sz=1')
    pdf_url_pattern = re.compile(r'cdep\.ro/comisii/(?P<committee>[^/]+)/pdf/')

    def __init__(self, session=None, pdf_session=None):
        super().__init__(session)
        self.pdf_session = pdf_session or self.session

    def fetch_summaries(self, year=2013, get_pdf_text=False):
        from collections import defaultdict
        for p in range(50):
            page_url = self.listing_page_url.format(offset=100*p, year=year)
            page = self.fetch_url(page_url)
            i_el = list(pqitems(page, ":contains('înregistrări')"))[-1]
            table = list(i_el.parents('table'))[-1]
            empty_page = True
            table_rows = pqitems(pq(table), 'tr')
            assert "înregistrări găsite:" in next(table_rows).text()
            assert next(table_rows).text() == "Nr. Crt. PDF Data Titlu Comisia"
            for tr in table_rows:
                empty_page = False
                [pdf_link] = pqitems(tr, 'a[target=PDF]')
                col3 = list(pqitems(tr, 'td'))[2]
                date_value = datetime.strptime(col3.text(), '%d.%m.%Y').date()
                col4 = list(pqitems(tr, 'td'))[3]
                title = col4.text()
                col5 = list(pqitems(tr, 'td'))[4]
                pdf_url = pdf_link.attr('href')
                pdf_url_m = self.pdf_url_pattern.search(pdf_url)
                assert pdf_url_m is not None, "can't parse url: %r" % pdf_url
                committee_code = pdf_url_m.group('committee')
                assert committee_names[committee_code] == col5.text()
                row = {
                    'committee': committee_code,
                    'pdf_url': pdf_url,
                    'date': date_value,
                    'title': title,
                }

                if get_pdf_text:
                    pdf_data = self.pdf_session.get(pdf_url).content
                    text = pdf_to_text(pdf_data)
                    row['text'] = text

                yield row

            if empty_page:
                break
