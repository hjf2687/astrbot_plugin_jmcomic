"""
JMComic API 服务器（插件内置版）
"""

from flask import Flask, request, jsonify, send_file
import jmcomic
import os
import shutil
import glob
import random
from PIL import Image

app = Flask(__name__)

# 所有路径都基于本文件所在目录
DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DIR, 'option.yml')
DOWNLOAD_DIR = os.path.join(DIR, 'jm_comics')
TEMP_DIR = os.path.join(DIR, 'temp')


def init_dirs():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)


def get_option():
    option = jmcomic.JmOption.from_file(CONFIG_FILE)
    # 强制设置 base_dir 为插件目录
    option.dir_rule.base_dir = DOWNLOAD_DIR
    option.download.dir = DOWNLOAD_DIR
    return option


@app.route('/')
def index():
    return jsonify({'status': 'running'})


@app.route('/pdf/<album_id>')
def download_album_pdf(album_id):
    try:
        pdf_path = os.path.join(TEMP_DIR, f'{album_id}.pdf')
        if os.path.exists(pdf_path):
            resp = send_file(pdf_path, mimetype='application/pdf', as_attachment=True, download_name=f'{album_id}.pdf')
            resp.headers['X-Cached'] = 'true'
            return resp

        # 记录下载前的文件
        before = set(glob.glob(os.path.join(DOWNLOAD_DIR, '**', '*.*'), recursive=True))

        # 下载
        option = get_option()
        jmcomic.download_album(album_id, option=option)

        # 找新增文件
        after = set(glob.glob(os.path.join(DOWNLOAD_DIR, '**', '*.*'), recursive=True))
        images = sorted([f for f in (after - before) if f.lower().endswith(('.webp', '.jpg', '.jpeg', '.png'))])

        if not images:
            return jsonify({'error': '未找到图片'}), 404

        # 转 PDF
        first = Image.open(images[0]).convert('RGB')
        others = [Image.open(img).convert('RGB') for img in images[1:]]
        first.save(pdf_path, 'PDF', save_all=True, append_images=others)
        first.close()
        for img in others:
            img.close()

        # 删除原图
        for img_path in images:
            try:
                os.remove(img_path)
            except:
                pass

        # 删除空文件夹
        for img_path in images:
            folder = os.path.dirname(img_path)
            if folder and os.path.exists(folder) and not os.listdir(folder):
                try:
                    os.rmdir(folder)
                except:
                    pass

        return send_file(pdf_path, mimetype='application/pdf', as_attachment=True, download_name=f'{album_id}.pdf')

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/random')
def random_album():
    """随机获取周榜漫画"""
    try:
        option = get_option()
        client = option.new_jm_client()

        # 获取周榜，多取几页增加随机性
        all_albums = []
        for page in range(1, 4):
            try:
                page_result = client.week_ranking(page=page)
                for album_id, title in page_result.iter_id_title():
                    all_albums.append({'id': album_id, 'title': title})
            except:
                break

        if not all_albums:
            return jsonify({'error': '获取榜单失败'}), 500

        # 随机选一个
        chosen = random.choice(all_albums)
        return jsonify(chosen)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/search_random/<keyword>')
def search_random_album(keyword):
    """搜索关键词后随机获取一个漫画"""
    try:
        option = get_option()
        client = option.new_jm_client()

        # 搜索关键词，取多页
        all_albums = []
        for page in range(1, 4):
            try:
                page_result = client.search_tag(search_query=keyword, page=page)
                for album_id, title in page_result.iter_id_title():
                    all_albums.append({'id': album_id, 'title': title})
            except:
                break

        if not all_albums:
            return jsonify({'error': f'未找到"{keyword}"相关漫画'}), 404

        # 随机选一个
        chosen = random.choice(all_albums)
        return jsonify(chosen)

    except Exception as e:
        return jsonify({'error': str(e)}), 500
