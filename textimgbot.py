#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Telegram Text Image Render Bot
'''

import os
import sys
import time
import json
import queue
import base64
import logging
import hashlib
import requests
import tempfile
import functools
import threading
import subprocess
import collections
import concurrent.futures

logging.basicConfig(stream=sys.stderr, format='%(asctime)s [%(name)s:%(levelname)s] %(message)s', level=logging.DEBUG if sys.argv[-1] == '-v' else logging.INFO)

logger_botapi = logging.getLogger('botapi')
logger_inkscape = logging.getLogger('inkscape')

executor = concurrent.futures.ThreadPoolExecutor(5)
inkscape_executor = concurrent.futures.ThreadPoolExecutor(4)
HSession = requests.Session()

template_cache = None

class AttrDict(dict):

    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self


hashstr = lambda s: base64.urlsafe_b64encode(
    hashlib.sha256(s.encode('utf-8')).digest()).decode('utf-8').rstrip('=')

def hashfile(filename):
    hash_obj = hashlib.new('sha256')
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            hash_obj.update(chunk)
    return base64.urlsafe_b64encode(hash_obj.digest()).decode('utf-8').rstrip('=')

# Bot API

class BotAPIFailed(Exception):
    pass

def async_func(func):
    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        def func_noerr(*args, **kwargs):
            try:
                func(*args, **kwargs)
            except Exception:
                logger_botapi.exception('Async function failed.')
        executor.submit(func_noerr, *args, **kwargs)
    return wrapped

def bot_api(method, **params):
    for att in range(3):
        try:
            req = HSession.post(('https://api.telegram.org/bot%s/' %
                                CFG.apitoken) + method, data=params, timeout=45)
            retjson = req.content
            if not retjson:
                continue
            ret = json.loads(retjson.decode('utf-8'))
            break
        except Exception as ex:
            if att < 1:
                time.sleep((att + 1) * 2)
            else:
                raise ex
    if not ret['ok']:
        raise BotAPIFailed(repr(ret))
    return ret['result']

@async_func
def sendmsg(text, chat_id, reply_to_message_id=None, **kwargs):
    text = text.strip()
    if not text:
        logger_botapi.warning('Empty message ignored: %s, %s' % (chat_id, reply_to_message_id))
        return
    logger_botapi.debug('sendMessage(%s): %s' % (len(text), text[:20]))
    if len(text) > 2000:
        text = text[:1999] + '…'
    reply_id = reply_to_message_id
    if reply_to_message_id and reply_to_message_id < 0:
        reply_id = None
    return bot_api('sendMessage', chat_id=chat_id, text=text,
                   reply_to_message_id=reply_id, **kwargs)

@async_func
def answer(inline_query_id, results, **kwargs):
    return bot_api('answerInlineQuery', inline_query_id=inline_query_id,
                   results=json.dumps(results), **kwargs)

def getupdates():
    global CFG, STATE
    while 1:
        try:
            updates = bot_api('getUpdates', offset=CFG.get('offset', 0), timeout=10)
        except Exception:
            logger_botapi.exception('Get updates failed.')
            continue
        if updates:
            CFG['offset'] = updates[-1]["update_id"] + 1
            for upd in updates:
                MSG_Q.put(upd)
        time.sleep(.2)

def parse_cmd(text: str):
    t = text.strip().replace('\xa0', ' ').split(' ', 1)
    if not t:
        return (None, None)
    cmd = t[0].rsplit('@', 1)
    if len(cmd[0]) < 2 or cmd[0][0] != "/":
        return (None, None)
    if len(cmd) > 1 and 'username' in CFG and cmd[-1] != CFG.username:
        return (None, None)
    expr = t[1] if len(t) > 1 else ''
    return (cmd[0][1:], expr.strip())

# Processing

def update_templates():
    global template_cache
    template_cache = collections.OrderedDict()
    for i in os.listdir(CFG['templates']):
        name, ext = os.path.splitext(i)
        if ext == '.svg':
            template_cache[name] = os.path.join(CFG['templates'], i)

def generate_image(templatefile, output, *args, **kwargs):
    with open(templatefile, 'r', encoding='utf-8') as f:
        template = f.read().format(*args, **kwargs)
    with tempfile.NamedTemporaryFile('w', suffix='.svg') as f:
        f.write(template)
        proc = subprocess.Popen(
            ('inkscape', '-z', '--export-background=white', '-e', output + '.png', f.name),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        try:
            outs, errs = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            outs, errs = proc.communicate()
        if proc.returncode != 0:
            logger_inkscape.error('Inkscape returns %s', proc.returncode)
            logger_inkscape.info(outs.decode())
            return False
        proc = subprocess.Popen(
            ('convert', output + '.png', output),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        try:
            outs, errs = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            outs, errs = proc.communicate()
        try:
            os.unlink(output + '.png')
        except FileNotFoundError:
            pass
        if proc.returncode != 0:
            logger_inkscape.error('Convert returns %s', proc.returncode)
            logger_inkscape.info(outs.decode())
            return False
        return True

def render_images(text):
    args = [text] + text.split('/')
    ret = []
    for template, templatefile in template_cache.items():
        fileid = hashstr('%s|%s' % (template, text))
        filepath = os.path.join(CFG['images'], fileid + '.jpg')
        if os.path.isfile(filepath):
            ret.append(fileid)
        else:
            success = generate_image(templatefile, filepath, *args)
            if success:
                ret.append(fileid)
    return ret

# Query handling

START = 'This is the Text Image Render Bot. Send /help, or directly use its inline mode.'

HELP = (
    'You can type text for images in its inline mode, seperate parameters by "/".\n'
    'You can add your SVG template by sending SVG files, delete your template by '
    '/delsvg [id]. The SVG must have Python str.format code ({0} is the full text, '
    '{1} and so on are the parameters), and must be compatible with Inkscape.'
)

def handle_api_update(d: dict):
    logger_botapi.debug('Update: %r' % d)
    try:
        if 'inline_query' in d:
            query = d['inline_query']
            text = query['query'].strip()
            if text:
                images = render_images(text)
                logging.info('Rendered: %s', text)
                r = answer(query['id'], inline_result(images))
                logger_botapi.debug(r)
        elif 'message' in d:
            msg = d['message']
            text = msg.get('text', '')
            document = msg.get('document')
            ret = None
            if document:
                on_document(document, msg['chat'], msg)
            elif text:
                cmd, expr = parse_cmd(text)
                if msg['chat']['type'] == 'private':
                    if cmd == 'start':
                        ret = START
                    elif cmd == 'delsvg':
                        ret = cmd_delsvg(text, msg['chat'], msg['message_id'], msg)
                    else:
                        ret = HELP
            if ret:
                sendmsg(ret, msg['chat']['id'], msg['message_id'])
    except Exception:
        logger_botapi.exception('Failed to process a message.')

def inline_result(images):
    ret = []
    for d in images:
        ret.append({
            'type': 'photo',
            'id': d,
            'photo_url': CFG['urlroot'] + d + '.jpg',
            'thumb_url': CFG['urlroot'] + d + '.jpg',
        })
    return ret

def cmd_delsvg(expr, chat, replyid, msg):
    if chat['type'] == 'private':
        return "Not Implemented."

def on_document(document, chat, msg):
    if chat['type'] == 'private':
        return "Not Implemented."

def load_config():
    return AttrDict(json.load(open('config.json', encoding='utf-8')))

def save_config(config):
    json.dump(config, open('config.json', 'w'), sort_keys=True, indent=1)

if __name__ == '__main__':
    CFG = load_config()
    MSG_Q = queue.Queue()
    update_templates()
    apithr = threading.Thread(target=getupdates)
    apithr.daemon = True
    apithr.start()
    logging.info('Satellite launched')
    while 1:
        handle_api_update(MSG_Q.get())
