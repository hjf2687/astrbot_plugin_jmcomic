# JMComic 下载插件

为 AstrBot 提供漫画下载功能，下载后自动转换为 PDF。

## 功能

- 输入 jm漫画ID 即可下载
- 自动转换为 PDF 格式
- 自动清理原始图片，节省空间
- 支持缓存，已下载的漫画直接发送
- API 服务器集成在插件中，无需手动启动

## 使用方法

```
jm350234      下载漫画
jm摇号        随机获取日榜漫画
jm摇号人妻    搜索关键词后随机获取
jmhelp        显示帮助
```

## 文件说明

```
astrbot_plugin_jmcomic/
├── main.py           插件主文件
├── jm_api_server.py  内置 API 服务器
├── option.yml        JMComic 配置
├── jm_comics/        漫画下载目录
├── temp/             PDF 缓存目录
├── metadata.yaml     插件元数据
├── README.md         说明文档
└── _conf_schema.json 配置 schema
```

## 配置

option.yml 中可配置代理等参数：

```yaml
# 代理配置（如需使用代理，取消注释并填写代理地址）
# proxy:
#   - 'http://127.0.0.1:7890'
```

## 更新日志

### v2.1.0
- 新增摇号功能：`jm摇号` 随机获取日榜漫画
- 新增关键词摇号：`jm摇号<关键词>` 搜索后随机获取