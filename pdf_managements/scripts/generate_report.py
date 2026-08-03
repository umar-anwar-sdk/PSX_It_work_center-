import urllib.request, http.cookiejar, urllib.parse, re, sys
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
url='http://127.0.0.1:8000/pages/reports/'
try:
    r = opener.open(url)
    html = r.read().decode('utf-8')
    m = re.search(r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)['\"]", html)
    if not m:
        print('No CSRF token found')
        sys.exit(1)
    token = m.group(1)
    data = urllib.parse.urlencode({'report_type':'daily','csrfmiddlewaretoken':token}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Referer', url)
    resp = opener.open(req)
    html2 = opener.open(url).read().decode('utf-8')
    print('REPORTS_PAGE_LENGTH', len(html2))
    if 'Download' in html2:
        print('Found Download link')
    else:
        print('No Download link found')
    start = html2.find('<tbody>')
    print(html2[start:start+500])
except Exception as e:
    print('ERROR', type(e).__name__, e)
    raise
