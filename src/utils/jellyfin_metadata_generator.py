"""
Jellyfin元数据生成器 - 将FC2视频分析结果转换为Jellyfin兼容的元数据格式

提供对分析结果的处理，生成Jellyfin可识别的NFO元数据文件和图像文件，
以便在Jellyfin媒体服务器中正确显示FC2视频信息。
"""

import os
import shutil
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
import re
import asyncio
import aiohttp
import time
import random
from bs4 import BeautifulSoup
from datetime import datetime

from config import config, BASE_CACHE_DIR
from src.utils.logger import get_logger
from src.utils.i18n import get_text as _

# 获取日志记录器
logger = get_logger("jellyfin_metadata")

class JellyfinMetadataGenerator:
    """将FC2视频信息转换为Jellyfin元数据格式"""
    
    def __init__(self, output_dir=None):
        """初始化元数据生成器
        
        Args:
            output_dir: 元数据输出目录，默认为data/jellyfin
        """
        self.output_dir = output_dir or os.path.join(BASE_CACHE_DIR, "jellyfin")
        os.makedirs(self.output_dir, exist_ok=True)
        
        logger.info(_("jellyfin.initialize").format(path=self.output_dir))
        
        # FC2PPVDB 网站的基础URL
        self.fc2ppvdb_base_url = "https://fc2ppvdb.com/articles"
        
        # 设置重试和退避机制参数
        self.max_retries = config.max_retries
        self.base_timeout = config.timeout
        self.min_wait_time = 5.0  # 最小等待时间（秒）
        self.max_wait_time = 6.0  # 最大等待时间（秒）- 确保批次间等待不超过6秒
        
        # 创建作者和女优子目录
        self.authors_dir = os.path.join(self.output_dir, "authors")
        self.actresses_dir = os.path.join(self.output_dir, "actresses")
        os.makedirs(self.authors_dir, exist_ok=True)
        os.makedirs(self.actresses_dir, exist_ok=True)
        
        # 429错误计数器
        self.rate_limit_count = 0
        # 429错误阈值，超过此值将切换到单线程模式
        self.rate_limit_threshold = 10
        # 429错误阈值，超过此值将跳过网络请求
        self.skip_network_threshold = 20

    async def fetch_page(self, url):
        """获取页面HTML内容，带重试和退避机制
        
        Args:
            url: 网页URL
            
        Returns:
            str: 页面HTML内容，失败返回None
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        
        for attempt in range(1, self.max_retries + 1):
            try:
                timeout = self.base_timeout * (1 + (attempt - 1) * 0.5)  # 递增超时时间
                
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url, timeout=timeout) as response:
                        if response.status == 200:
                            return await response.text()
                        
                        # 处理常见错误状态码
                        if response.status == 429 or response.status >= 500:
                            # 增加429错误计数
                            if response.status == 429:
                                self.rate_limit_count += 1
                                
                            # 计算等待时间并睡眠
                            wait_time = self._calculate_wait_time(attempt)
                            
                            logger.warning(_("logger.rate_limit").format(
                                status_code=response.status,
                                wait_time=wait_time
                            ))
                            
                            await asyncio.sleep(wait_time)
                            continue
                        
                        logger.warning(_("jellyfin.page_fetch_failed").format(status_code=response.status, url=url))
                        return None
                        
            except asyncio.TimeoutError:
                wait_time = self._calculate_wait_time(attempt)
                logger.warning(f"请求超时，等待 {wait_time:.2f} 秒后重试 ({attempt}/{self.max_retries})")
                await asyncio.sleep(wait_time)
                
            except Exception as e:
                wait_time = self._calculate_wait_time(attempt)
                logger.error(f"获取页面异常: {str(e)}, URL: {url}")
                logger.warning(f"等待 {wait_time:.2f} 秒后重试 ({attempt}/{self.max_retries})")
                await asyncio.sleep(wait_time)
                
        logger.error(f"达到最大重试次数 ({self.max_retries})，获取页面失败: {url}")
        return None

    def _calculate_wait_time(self, attempt):
        """计算重试等待时间
        
        Args:
            attempt: 当前尝试次数
            
        Returns:
            float: 计算后的等待时间（秒）
        """
        # 使用指数退避
        wait_time = min(self.max_wait_time, self.min_wait_time * (2 ** (attempt - 1)))
        # 添加随机抖动以避免请求同步
        wait_time = wait_time * (0.5 + random.random())
        # 确保等待时间不超过最大值
        wait_time = min(wait_time, self.max_wait_time)
        
        return wait_time

    def parse_html(self, html_content, fc2_id):
        """解析HTML内容提取视频信息
        
        Args:
            html_content: HTML内容
            fc2_id: FC2视频ID
            
        Returns:
            dict: 解析得到的视频信息
        """
        if not html_content:
            return {}
            
        results = {'fc2_id': fc2_id, 'tags': []}
        
        # 提取标签
        self._extract_tags(html_content, results)
        
        # 提取马赛克状态
        self._extract_with_pattern(
            html_content, 
            r'<ruby>モザイク<rt[^>]*>[^<]*</rt></ruby>：<span[^>]*>([^<]+)</span>', 
            'mosaic_type', 
            results
        )
        
        # 提取发售日
        self._extract_with_pattern(
            html_content, 
            r'販売日：<span[^>]*>([^<]+)</span>', 
            'release_date', 
            results
        )
        
        # 提取视频长度
        self._extract_with_pattern(
            html_content, 
            r'収録時間：<span[^>]*>([^<]+)</span>', 
            'duration', 
            results
        )
            
        # 获取标题
        self._extract_with_pattern(
            html_content, 
            r'<h2[^>]*>.*?<a[^>]*>([^<]+)</a>', 
            'title', 
            results, 
            re.DOTALL
        )
        
        return results
        
    def _extract_tags(self, html_content, results):
        """提取标签信息
        
        Args:
            html_content: HTML内容
            results: 结果字典，将直接修改
        """
        # 方法1：使用正则表达式
        tag_section_pattern = re.compile(r'<div[^>]*>(?:<ruby>)?タグ(?:<rt[^>]*>[^<]*</rt></ruby>)?[^:：]*[:：]\s*<span[^>]*>(.*?)</span>(?:</div>)?', re.DOTALL)
        tag_section_match = tag_section_pattern.search(html_content)
        if tag_section_match:
            tags_content = tag_section_match.group(1)
            tag_link_pattern = re.compile(r'<a[^>]*href="/tags/\?name=([^"&]+)[^"]*"[^>]*>([^<]+)</a>')
            tag_matches = tag_link_pattern.finditer(tags_content)
            for tag_match in tag_matches:
                tag_name = tag_match.group(2)
                results['tags'].append(tag_name)
        
        # 方法2：如果正则表达式失败，尝试使用BeautifulSoup
        if not results['tags']:
            try:
                soup = BeautifulSoup(html_content, 'html.parser')
                # 查找包含"タグ"的div
                for div in soup.find_all('div'):
                    if 'タグ' in div.text:
                        # 只查找href包含"/tags/"的链接
                        tag_links = [a for a in div.find_all('a') if 'href' in a.attrs and '/tags/' in a['href']]
                        for tag_link in tag_links:
                            tag_name = tag_link.text.strip()
                            if tag_name and tag_name not in results['tags']:
                                results['tags'].append(tag_name)
                        
                        # 如果找到了标签所在的div，就跳出循环
                        if results['tags']:
                            break
            except Exception as e:
                logger.error(f"使用BeautifulSoup解析标签失败: {str(e)}")
        
    def _extract_with_pattern(self, html_content, pattern, key, results, flags=0):
        """使用正则表达式提取内容
        
        Args:
            html_content: HTML内容
            pattern: 正则表达式模式
            key: 存储提取内容的键名
            results: 结果字典，将直接修改
            flags: 正则表达式标志
        """
        regex = re.compile(pattern, flags)
        match = regex.search(html_content)
        if match:
            results[key] = match.group(1)

    async def enrich_video_info(self, video_info):
        """从FC2PPVDB获取额外的视频信息
        
        Args:
            video_info: 原始视频信息字典
            
        Returns:
            dict: 增强后的视频信息
        """
        video_id = video_info.get("video_id")
        if not video_id:
            logger.warning("无法获取额外信息：视频ID不存在")
            return video_info
            
        # 尝试从磁链缓存文件中获取磁链信息
        magnets = self._get_magnets_from_cache(video_id, video_info)
        if magnets:
            video_info["magnets"] = magnets
            logger.info(f"从缓存中获取到视频 {video_id} 的磁链：{len(magnets)}个")
            
        # 如果429错误次数超过阈值，跳过网络请求
        if self.rate_limit_count >= self.skip_network_threshold:
            logger.warning(f"429错误次数({self.rate_limit_count})超过阈值({self.skip_network_threshold})，跳过网络请求获取标签信息")
            return video_info
            
        logger.info(_("jellyfin.fetch_extra_info").format(video_id=video_id))
        
        # 构造FC2PPVDB URL
        url = f"{self.fc2ppvdb_base_url}/{video_id}"
        
        # 获取页面内容
        html_content = await self.fetch_page(url)
        if not html_content:
            logger.warning(_("jellyfin.fetch_failed").format(url=url))
            return video_info
            
        # 解析页面内容
        extra_info = self.parse_html(html_content, video_id)
        if not extra_info:
            logger.warning(f"无法从FC2PPVDB页面解析额外信息: {url}")
            return video_info
            
        # 合并信息，优先使用原始信息
        enriched_info = {**extra_info, **video_info}
        
        # 标签特殊处理：合并标签
        if extra_info.get("tags") and video_info.get("tags"):
            all_tags = set(video_info["tags"]) | set(extra_info["tags"])
            enriched_info["tags"] = list(all_tags)
        
        logger.info(_("jellyfin.fetch_success").format(video_id=video_id))
        return enriched_info
        
    def _get_magnets_from_cache(self, video_id, video_info):
        """从缓存文件中获取磁链信息
        
        Args:
            video_id: 视频ID
            video_info: 视频信息字典
        
        Returns:
            list: 磁链列表
        """
        # 如果视频信息中已有磁链，直接返回
        if video_info.get("magnets") and isinstance(video_info.get("magnets"), list):
            return video_info.get("magnets")
            
        if video_info.get("magnet"):
            return [video_info.get("magnet")]
            
        # 尝试从results目录中查找磁链缓存文件
        try:
            # 检查是否有作者或女优信息
            author_id = None
            actress_id = None
            
            if "author_id" in video_info:
                author_id = video_info["author_id"]
            
            if "actress_id" in video_info:
                actress_id = video_info["actress_id"]
                
            # 查找可能的磁链文件路径模式
            possible_paths = []
            
            # 添加基本模式
            basic_paths = [
                os.path.join(config.result_dir, f"*_{video_id}_磁链.txt"),
                os.path.join(config.result_dir, f"*_{video_id}_magnet.txt"),
                os.path.join(config.magnet_dir, f"{video_id}.txt"),
                os.path.join(config.magnet_dir, f"FC2-PPV-{video_id}.txt")
            ]
            possible_paths.extend(basic_paths)
            
            # 添加作者/女优相关模式 - 包括各种可能的格式
            if author_id:
                author_patterns = [
                    os.path.join(config.result_dir, f"author_{author_id}*_磁链.txt"),
                    os.path.join(config.result_dir, f"author_{author_id}*_magnet.txt"),
                    os.path.join(config.result_dir, f"{author_id}_*_磁链.txt"),
                    os.path.join(config.result_dir, f"{author_id}_*_magnet.txt")
                ]
                possible_paths.extend(author_patterns)
            
            if actress_id:
                actress_patterns = [
                    os.path.join(config.result_dir, f"actress_{actress_id}*_磁链.txt"),
                    os.path.join(config.result_dir, f"actress_{actress_id}*_magnet.txt"),
                    os.path.join(config.result_dir, f"{actress_id}_*_磁链.txt"),
                    os.path.join(config.result_dir, f"{actress_id}_*_magnet.txt")
                ]
                possible_paths.extend(actress_patterns)
            
            # 搜索匹配的文件
            import glob
            matched_files = []
            for path_pattern in possible_paths:
                matched_files.extend(glob.glob(path_pattern))
            
            # 如果没有找到匹配的文件，尝试直接在results目录下查找包含"磁链"的文件
            if not matched_files:
                all_magnet_files = glob.glob(os.path.join(config.result_dir, "*_磁链.txt"))
                matched_files.extend(all_magnet_files)
                
                # 日志所有找到的磁链文件，帮助调试
                if all_magnet_files:
                    logger.info(f"找到的所有磁链文件: {all_magnet_files}")
            
            # 如果找到了匹配的文件
            magnets = []
            for file_path in matched_files:
                try:
                    logger.info(f"尝试从文件读取磁链: {file_path}")
                    with open(file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        
                    for i, line in enumerate(lines):
                        line = line.strip()
                        
                        # 尝试各种格式匹配
                        # 1. 以 "# {video_id} |" 开头的行
                        if line.startswith(f"# {video_id} |") or line.startswith(f"#{video_id} |"):
                            # 下一行应该是磁链
                            if i + 1 < len(lines) and lines[i + 1].strip().startswith("magnet:?"):
                                magnet = lines[i + 1].strip()
                                magnets.append(magnet)
                                logger.info(f"从文件 {file_path} 中找到视频 {video_id} 的磁链 (格式1)")
                                break
                        
                        # 2. 直接是以 "magnet:?" 开头的行，且文件名中包含视频ID
                        elif line.startswith("magnet:?") and f"{video_id}" in file_path:
                            magnets.append(line)
                            logger.info(f"从文件 {file_path} 中找到视频的磁链 (格式2)")
                        
                        # 3. 任意包含视频ID的行之后的磁链
                        elif video_id in line and i + 1 < len(lines) and lines[i + 1].strip().startswith("magnet:?"):
                            magnet = lines[i + 1].strip()
                            magnets.append(magnet)
                            logger.info(f"从文件 {file_path} 中找到视频 {video_id} 的磁链 (格式3)")
                            break
                            
                    # 如果上面的匹配都失败了，再尝试遍历所有行找磁链
                    if not magnets:
                        # 读取整个文件内容
                        content = "".join(lines)
                        # 尝试找出视频ID后面的磁链
                        video_id_pattern = re.compile(f"#{video_id}.*?\\n(magnet:\\?.*?)\\n", re.DOTALL)
                        matches = video_id_pattern.findall(content)
                        if matches:
                            for match in matches:
                                magnets.append(match.strip())
                                logger.info(f"从文件 {file_path} 中找到视频 {video_id} 的磁链 (正则匹配)")
                except Exception as e:
                    logger.error(f"读取磁链缓存文件 {file_path} 出错: {str(e)}")
                    
            # 检查是否找到了磁链
            if magnets:
                logger.info(f"成功为视频 {video_id} 找到 {len(magnets)} 个磁链")
            else:
                logger.warning(f"未找到视频 {video_id} 的磁链")
                
            return magnets
        except Exception as e:
            logger.error(f"搜索磁链缓存文件时出错: {str(e)}")
            return []

    def is_leaked(self, video_info):
        """判断视频是否已泄露
        
        Args:
            video_info: 视频信息字典
                
        Returns:
            bool: 是否已泄露
        """
        # 如果leaked字段存在并且为True，直接返回True
        if video_info.get("leaked") is True:
            return True
        
        status = video_info.get("status")
        
        # 如果status是available或leaked，视为已泄露
        if status in ["available", "leaked", "已流出"]:
            return True
            
        # 如果status是布尔类型
        if isinstance(status, bool):
            return status
        
        # 默认为未泄露
        return False
    
    async def generate_metadata(self, video_info, image_path=None, author_info=None, actress_info=None, enrich_from_web=True):
        """为单个视频生成Jellyfin元数据
        
        Args:
            video_info: 视频信息字典
            image_path: 封面图片路径
            author_info: 作者信息字典
            actress_info: 女优信息字典
            enrich_from_web: 是否从网络获取额外信息
                
        Returns:
            str: 生成的NFO文件路径
        """
        # 确保视频ID存在
        if "video_id" not in video_info:
            logger.error("无法生成元数据：视频ID不存在")
            return None
        
        video_id = video_info.get("video_id")
        logger.info(_("jellyfin.generate_start").format(video_id=video_id))
        
        # 从网络获取额外信息
        if enrich_from_web:
            video_info = await self.enrich_video_info(video_info)
        else:
            # 即使不从网络获取信息，也尝试从本地缓存获取磁链
            magnets = self._get_magnets_from_cache(video_id, video_info)
            if magnets:
                video_info["magnets"] = magnets
                
        # 创建XML根元素
        root = ET.Element("movie")
        
        # 添加基本信息
        title = video_info.get("title", f"FC2-PPV-{video_id}")
        ET.SubElement(root, "title").text = title
        ET.SubElement(root, "originaltitle").text = f"FC2-PPV-{video_id}"
        ET.SubElement(root, "sorttitle").text = f"FC2-PPV-{video_id}"
        
        # 添加视频ID作为uniqueid
        uniqueid = ET.SubElement(root, "uniqueid", type="fc2ppv", default="true")
        uniqueid.text = video_id
        
        # 添加发布日期
        release_date = video_info.get("release_date") or video_info.get("publish_date")
        if release_date:
            ET.SubElement(root, "premiered").text = release_date
            ET.SubElement(root, "releasedate").text = release_date
            
            # 提取年份
            try:
                year = release_date.split("-")[0]
                ET.SubElement(root, "year").text = year
            except (IndexError, AttributeError):
                pass
        
        # 添加运行时间
        if "duration" in video_info:
            # 尝试将时长转换为分钟数
            try:
                duration_str = video_info["duration"]
                if "分" in duration_str:
                    minutes = int(re.search(r'(\d+)分', duration_str).group(1))
                    ET.SubElement(root, "runtime").text = str(minutes)
            except (AttributeError, ValueError):
                pass
            
        # 添加是否流出的信息到情节介绍
        plot_text = ""
            
        # 添加其他视频信息到情节介绍
        if "description" in video_info and video_info["description"]:
            plot_text += f"{video_info['description']}"
            
        # 添加马赛克类型
        if "mosaic_type" in video_info:
            plot_text += f"\n马赛克类型: {video_info['mosaic_type']}"
            
        # 添加观看链接
        plot_text += f"\n\n{_('jellyfin.watch_links')}\n"
        # 使用HTML <a>标签创建可点击链接
        plot_text += f'<a href="https://missav.ws/dm14/en/fc2-ppv-{video_id}">MissAV</a>: https://missav.ws/dm14/en/fc2-ppv-{video_id}\n'
        plot_text += f'<a href="https://123av.com/en/dm2/v/fc2-ppv-{video_id}">123AV</a>: https://123av.com/en/dm2/v/fc2-ppv-{video_id}\n'
            
        # 如果有磁力链接，添加到情节介绍
        magnets = video_info.get("magnets", []) or ([video_info.get("magnet")] if video_info.get("magnet") else [])
        if magnets:
            plot_text += "\n" + _("jellyfin.magnet_links").format() + "\n"
            for idx, magnet in enumerate(magnets, 1):
                if magnet:
                    # 创建可点击的磁链链接
                    plot_text += f'{idx}. <a href="{magnet}">{magnet}</a>\n'
                    
        # 添加情节介绍
        ET.SubElement(root, "plot").text = plot_text.strip()
        ET.SubElement(root, "outline").text = title
        
        # 添加预告片链接（在Jellyfin中显示为可点击按钮）
        # 确保使用Jellyfin和Kodi官方支持的格式
        trailer_element = ET.SubElement(root, "trailer")
        trailer_url = f"https://missav.ws/dm14/en/fc2-ppv-{video_id}"
        trailer_element.text = trailer_url
        
        # 添加锁定标记，防止元数据被覆盖
        ET.SubElement(root, "lockdata").text = "true"
        
        # 添加displaylinks标签用于显示链接
        displaylinks = ET.SubElement(root, "displaylinks")
        missav_display = ET.SubElement(displaylinks, "link")
        missav_display.text = f"https://missav.ws/dm14/en/fc2-ppv-{video_id}"
        missav_display.set("name", "MissAV")
        
        av123_display = ET.SubElement(displaylinks, "link")
        av123_display.text = f"https://123av.com/en/dm2/v/fc2-ppv-{video_id}"
        av123_display.set("name", "123AV")
        
        # 添加外部链接到moviedb部分
        moviedb = ET.SubElement(root, "moviedb")
        missav_link = ET.SubElement(moviedb, "missav")
        missav_link.text = f"fc2-ppv-{video_id}"
        
        av123_link = ET.SubElement(moviedb, "av123")
        av123_link.text = f"fc2-ppv-{video_id}"
        
        # 添加额外的URL元素
        url_missav = ET.SubElement(root, "url")
        url_missav.text = f"https://missav.ws/dm14/en/fc2-ppv-{video_id}"
        
        url_123av = ET.SubElement(root, "url")
        url_123av.text = f"https://123av.com/en/dm2/v/fc2-ppv-{video_id}"
        
        # 添加制作公司/作者信息
        if author_info and "name" in author_info:
            ET.SubElement(root, "studio").text = author_info["name"]
        elif "author_name" in video_info:
            ET.SubElement(root, "studio").text = video_info["author_name"]
            
        # 添加导演信息(使用作者名称)
        director = ET.SubElement(root, "director")
        if author_info and "name" in author_info:
            director.text = author_info["name"]
        elif "author_name" in video_info:
            director.text = video_info["author_name"]
        else:
            director.text = "Unknown"
        
        # 添加演员信息
        if actress_info and "name" in actress_info:
            actor = ET.SubElement(root, "actor")
            ET.SubElement(actor, "name").text = actress_info["name"]
        elif "actress_name" in video_info:
            actor = ET.SubElement(root, "actor")
            ET.SubElement(actor, "name").text = video_info["actress_name"]
        
        # 添加标签/分类
        ET.SubElement(root, "genre").text = "FC2"
        
        # 添加马赛克类型作为标签
        if "mosaic_type" in video_info:
            ET.SubElement(root, "genre").text = video_info["mosaic_type"]
            
        # 添加其他标签
        if video_info.get("tags"):
            for tag in video_info["tags"]:
                ET.SubElement(root, "genre").text = tag
                
        # 添加特殊标签
        ET.SubElement(root, "tag").text = "FC2"
        
        # 添加播放源链接 - 方法1：使用fileinfo和streamdetails标签
        fileinfo = ET.SubElement(root, "fileinfo")
        streamdetails = ET.SubElement(fileinfo, "streamdetails")
        
        # 添加MissAV链接
        missav_stream = ET.SubElement(streamdetails, "video")
        ET.SubElement(missav_stream, "provider").text = "MissAV"
        ET.SubElement(missav_stream, "url").text = f"https://missav.ws/dm14/en/fc2-ppv-{video_id}"
        
        # 添加123AV链接
        av123_stream = ET.SubElement(streamdetails, "video")
        ET.SubElement(av123_stream, "provider").text = "123AV"
        ET.SubElement(av123_stream, "url").text = f"https://123av.com/en/dm2/v/fc2-ppv-{video_id}"
        
        # 方法2：添加外部链接ID (用于Jellyfin中显示可点击按钮)
        missav_id = ET.SubElement(root, "uniqueid", type="missav")
        missav_id.text = f"fc2-ppv-{video_id}"
        
        av123_id = ET.SubElement(root, "uniqueid", type="123av")
        av123_id.text = f"fc2-ppv-{video_id}"
        
        # 方法3：添加外部链接信息
        # 使用标准格式添加链接 - 使用官方支持的格式
        ET.SubElement(root, "url").text = f"https://missav.ws/dm14/en/fc2-ppv-{video_id}"
        ET.SubElement(root, "url").text = f"https://123av.com/en/dm2/v/fc2-ppv-{video_id}"
        
        # 保存为美观格式的XML
        xml_str = minidom.parseString(ET.tostring(root, encoding='unicode')).toprettyxml(indent="  ")
        
        # 确定输出目录
        output_dir = self.output_dir  # 默认目录
        
        # 如果有作者信息，创建作者子目录
        if author_info and "id" in author_info and not actress_info:
            author_id = author_info["id"]
            author_name = author_info.get("name", "")
            
            # 清理作者名称以用于路径
            author_name_clean = self._clean_filename(author_name)
            
            # 创建作者子目录 (格式: 作者名_id)
            author_subdir = f"{author_name_clean}_{author_id}" if author_name_clean else f"author_{author_id}"
            author_dir = os.path.join(self.authors_dir, author_subdir)
            os.makedirs(author_dir, exist_ok=True)
            
            # 使用作者子目录作为输出目录
            output_dir = author_dir
            logger.info(_("jellyfin.using_author_dir").format(dir=author_dir))
            
        # 如果有女优信息，创建女优子目录
        elif actress_info and "id" in actress_info:
            actress_id = actress_info["id"]
            actress_name = actress_info.get("name", "")
            
            # 清理女优名称以用于路径
            actress_name_clean = self._clean_filename(actress_name)
            
            # 创建女优子目录 (格式: 女优名_id)
            actress_subdir = f"{actress_name_clean}_{actress_id}" if actress_name_clean else f"actress_{actress_id}"
            actress_dir = os.path.join(self.actresses_dir, actress_subdir)
            os.makedirs(actress_dir, exist_ok=True)
            
            # 使用女优子目录作为输出目录
            output_dir = actress_dir
            logger.info(_("jellyfin.using_actress_dir").format(dir=actress_dir))
        
        # 定义输出文件名，简化为只用视频ID
        output_filename = f"FC2-PPV-{video_id}"
        
        # 保存NFO文件
        nfo_path = os.path.join(output_dir, f"{output_filename}.nfo")
        try:
            with open(nfo_path, "w", encoding="utf-8") as f:
                f.write(xml_str)
            logger.info(_("jellyfin.save_metadata_success").format(path=nfo_path))
        except Exception as e:
            logger.error(_("jellyfin.save_metadata_failed").format(error=str(e)))
            return None
        
        # 处理图片
        poster_path = None
        if image_path and os.path.exists(image_path):
            try:
                # 获取图片扩展名
                image_ext = os.path.splitext(image_path)[1]
                if not image_ext:
                    image_ext = ".jpg"  # 默认使用jpg扩展名
                    
                # 设置目标路径，使用Jellyfin标准的-poster后缀
                poster_path = os.path.join(output_dir, f"{output_filename}-poster{image_ext}")
                
                # 复制图片
                shutil.copy(image_path, poster_path)
                logger.info(_("jellyfin.copy_poster_success").format(path=poster_path))
            except Exception as e:
                logger.error(_("jellyfin.copy_poster_failed").format(error=str(e)))
                poster_path = None
        
        # 创建空的MP4文件作为占位符
        mp4_path = os.path.join(output_dir, f"{output_filename}.mp4")
        try:
            # 创建0字节的空MP4文件
            with open(mp4_path, "wb") as f:
                pass
            logger.info(_("jellyfin.create_placeholder_success").format(path=mp4_path))
        except Exception as e:
            logger.error(_("jellyfin.create_placeholder_failed").format(error=str(e)))
            mp4_path = None
                
        return {
            "nfo_path": nfo_path,
            "poster_path": poster_path,
            "mp4_path": mp4_path,
            "video_id": video_id
        }
    
    def find_image_path(self, video_id, video_info, author_info=None, actress_info=None):
        """查找视频的图片路径
        
        Args:
            video_id: 视频ID
            video_info: 视频信息字典
            author_info: 作者信息字典
            actress_info: 女优信息字典
                
        Returns:
            str: 图片路径，如果找不到则返回None
        """
        # 尝试不同的图片路径模式
        possible_paths = []
        
        # 1. 直接在img目录下查找
        possible_paths.extend([
            os.path.join(config.image_dir, f"{video_id}.jpg"),
            os.path.join(config.image_dir, f"FC2-PPV-{video_id}.jpg")
        ])
        
        # 2. 作者目录下查找
        if author_info and "id" in author_info:
            self._add_entity_image_paths(
                possible_paths, 
                video_id, 
                "author", 
                author_info["id"], 
                author_info.get("name", "")
            )
        
        # 3. 女优目录下查找
        if actress_info and "id" in actress_info:
            self._add_entity_image_paths(
                possible_paths, 
                video_id, 
                "actress", 
                actress_info["id"], 
                actress_info.get("name", "")
            )
            
            # 特殊形式: actress_{id}_Actress_{id}
            actress_id = actress_info["id"]
            actress_dir_special = os.path.join(config.image_dir, f"actress_{actress_id}_Actress_{actress_id}")
            possible_paths.extend([
                os.path.join(actress_dir_special, f"{video_id}.jpg"),
                os.path.join(actress_dir_special, "leaked", f"{video_id}.jpg"),
                os.path.join(actress_dir_special, "unleaked", f"{video_id}.jpg")
            ])
        
        # 4. 尝试在data/img下查找任何可能包含该视频ID的图片
        self._add_recursive_image_paths(possible_paths, video_id)
        
        # 检查所有可能的路径
        for path in possible_paths:
            if os.path.exists(path):
                logger.info(_("jellyfin.found_image").format(video_id=video_id, path=path))
                return path
        
        # 如果没有找到图片，记录警告
        logger.warning(f"未找到视频 FC2-PPV-{video_id} 的图片")
        return None
    
    def _add_entity_image_paths(self, possible_paths, video_id, entity_type, entity_id, entity_name):
        """添加实体相关的图片路径
        
        Args:
            possible_paths: 路径列表，将直接修改
            video_id: 视频ID
            entity_type: 实体类型，如"author"或"actress"
            entity_id: 实体ID
            entity_name: 实体名称
        """
        if entity_name:
            # 清理名称以用于路径
            clean_name = self._clean_filename(entity_name)
            
            # 形如: {entity_type}_{id}_{name}
            entity_dir = os.path.join(config.image_dir, f"{entity_type}_{entity_id}_{clean_name}")
            possible_paths.extend([
                os.path.join(entity_dir, f"{video_id}.jpg"),
                os.path.join(entity_dir, "leaked", f"{video_id}.jpg"),
                os.path.join(entity_dir, "unleaked", f"{video_id}.jpg")
            ])
        else:
            # 只有ID的情况
            entity_dir = os.path.join(config.image_dir, f"{entity_type}_{entity_id}")
            possible_paths.extend([
                os.path.join(entity_dir, f"{video_id}.jpg"),
                os.path.join(entity_dir, "leaked", f"{video_id}.jpg"),
                os.path.join(entity_dir, "unleaked", f"{video_id}.jpg")
            ])
            
        # 尝试通配符匹配
        try:
            import glob
            entity_dir_pattern = os.path.join(config.image_dir, f"{entity_type}_{entity_id}_*")
            matched_dirs = glob.glob(entity_dir_pattern)
            for matched_dir in matched_dirs:
                if os.path.isdir(matched_dir):
                    # 添加各种可能的路径
                    possible_paths.extend([
                        os.path.join(matched_dir, f"{video_id}.jpg"),
                        os.path.join(matched_dir, "leaked", f"{video_id}.jpg"),
                        os.path.join(matched_dir, "unleaked", f"{video_id}.jpg")
                    ])
        except Exception as e:
            logger.error(f"搜索{entity_type}目录模式时出错: {str(e)}")
            
    def _add_recursive_image_paths(self, possible_paths, video_id):
        """递归查找图片路径
        
        Args:
            possible_paths: 路径列表，将直接修改
            video_id: 视频ID
        """
        try:
            import glob
            video_pattern = os.path.join(config.image_dir, "**", f"{video_id}.jpg")
            matched_files = glob.glob(video_pattern, recursive=True)
            for matched_file in matched_files:
                if os.path.isfile(matched_file):
                    possible_paths.append(matched_file)
        except Exception as e:
            logger.error(f"递归搜索图片文件时出错: {str(e)}")
            
    def _clean_filename(self, name):
        """清理文件名，移除不允许的字符
        
        Args:
            name: 原始文件名
            
        Returns:
            str: 清理后的文件名
        """
        if not name:
            return "unknown"
            
        # 移除Windows文件系统不支持的字符
        invalid_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in invalid_chars:
            name = name.replace(char, '_')
            
        # 移除前导和尾随空格
        name = name.strip()
        
        # 如果名称为空，使用默认名称
        if not name:
            return "unknown"
            
        return name 

    async def batch_generate_metadata(self, videos_info, author_info=None, actress_info=None, enrich_from_web=True):
        """批量生成多个视频的元数据
        
        Args:
            videos_info: 视频信息列表
            author_info: 作者信息字典
            actress_info: 女优信息字典
            enrich_from_web: 是否从网络获取额外信息
                
        Returns:
            list: 生成的元数据文件信息列表
        """
        if not videos_info:
            logger.warning("没有视频信息可用于生成元数据")
            return []
        
        # 过滤出已流出的视频
        leaked_videos = [video for video in videos_info if self.is_leaked(video)]
        
        # 如果没有已流出的视频，直接返回
        if not leaked_videos:
            logger.info(_("jellyfin.no_leaked_videos"))
            return []
            
        logger.info(_("jellyfin.start_batch").format(count=len(leaked_videos)))
        
        # 记录日志，显示当前处理的是作者还是女优
        self._log_entity_info(author_info, actress_info)
        
        # 初始化处理状态
        results = []
        self.rate_limit_count = 0
        use_single_thread = False
        skip_network_requests = False
        
        # 每批次处理的视频数量
        batch_size = 5
        total_batches = (len(leaked_videos) + batch_size - 1) // batch_size
        
        for batch_idx in range(total_batches):
            # 获取当前批次的视频
            start_idx = batch_idx * batch_size
            end_idx = min(start_idx + batch_size, len(leaked_videos))
            batch_videos = leaked_videos[start_idx:end_idx]
            
            logger.info(f"处理第 {batch_idx+1}/{total_batches} 批视频 ({len(batch_videos)}个)")
            
            # 检查是否需要更新处理模式
            use_single_thread, skip_network_requests, enrich_from_web = self._check_processing_mode(
                use_single_thread, skip_network_requests, enrich_from_web
            )
            
            # 处理当前批次视频
            batch_results = await self._process_batch(
                batch_videos, author_info, actress_info, 
                enrich_from_web, use_single_thread
            )
            results.extend(batch_results)
            
            # 处理批次间等待
            if batch_idx < total_batches - 1:
                await self._handle_batch_wait(use_single_thread, skip_network_requests)
                
        logger.info(_("jellyfin.generate_complete").format(count=len(results)))
        return results
        
    def _log_entity_info(self, author_info, actress_info):
        """记录实体信息日志
        
        Args:
            author_info: 作者信息字典
            actress_info: 女优信息字典
        """
        if author_info and "id" in author_info:
            logger.info(f"处理作者ID: {author_info['id']}, 名称: {author_info.get('name', '未知')}")
        elif actress_info and "id" in actress_info:
            logger.info(f"处理女优ID: {actress_info['id']}, 名称: {actress_info.get('name', '未知')}")
    
    def _check_processing_mode(self, use_single_thread, skip_network_requests, enrich_from_web):
        """检查并更新处理模式
        
        Args:
            use_single_thread: 当前是否使用单线程模式
            skip_network_requests: 当前是否跳过网络请求
            enrich_from_web: 当前是否从网络获取额外信息
            
        Returns:
            tuple: 更新后的(use_single_thread, skip_network_requests, enrich_from_web)
        """
        # 检查是否需要切换到单线程模式
        if self.rate_limit_count >= self.rate_limit_threshold and not use_single_thread:
            logger.warning(f"429错误次数达到阈值({self.rate_limit_count}/{self.rate_limit_threshold})，切换到单线程模式")
            use_single_thread = True
        
        # 检查是否需要跳过网络请求
        if self.rate_limit_count >= self.skip_network_threshold and not skip_network_requests:
            logger.warning(f"429错误次数达到阈值({self.rate_limit_count}/{self.skip_network_threshold})，跳过网络请求获取标签信息")
            skip_network_requests = True
            # 如果跳过网络请求，则不需要从网络获取额外信息
            enrich_from_web = False
            
        return use_single_thread, skip_network_requests, enrich_from_web
        
    async def _process_batch(self, batch_videos, author_info, actress_info, enrich_from_web, use_single_thread):
        """处理一批视频
        
        Args:
            batch_videos: 视频信息列表
            author_info: 作者信息字典
            actress_info: 女优信息字典
            enrich_from_web: 是否从网络获取额外信息
            use_single_thread: 是否使用单线程模式
            
        Returns:
            list: 有效的处理结果列表
        """
        results = []
        
        if use_single_thread:
            # 单线程模式：逐个处理视频
            for video_info in batch_videos:
                video_id = video_info.get("video_id")
                if not video_id:
                    logger.warning("跳过无效的视频信息(缺少video_id)")
                    continue
                    
                # 查找对应的图片路径
                image_path = self.find_image_path(video_id, video_info, author_info, actress_info)
                
                # 创建生成元数据的任务
                result = await self.generate_metadata(video_info, image_path, author_info, actress_info, enrich_from_web)
                if result:
                    results.append(result)
                
                # 如果已经跳过网络请求，则不需要等待
                if self.rate_limit_count >= self.skip_network_threshold:
                    # 直接处理下一个，不等待
                    continue
                else:
                    # 单线程模式下，每个请求之间添加等待时间
                    wait_time = 2.0  # 固定为2秒
                    logger.info(f"单线程模式：等待 {wait_time} 秒后处理下一个视频...")
                    await asyncio.sleep(wait_time)
        else:
            # 多线程模式：批量处理视频
            tasks = []
            for video_info in batch_videos:
                video_id = video_info.get("video_id")
                if not video_id:
                    logger.warning("跳过无效的视频信息(缺少video_id)")
                    continue
                    
                # 查找对应的图片路径
                image_path = self.find_image_path(video_id, video_info, author_info, actress_info)
                
                # 创建生成元数据的任务
                task = self.generate_metadata(video_info, image_path, author_info, actress_info, enrich_from_web)
                tasks.append(task)
            
            if tasks:
                # 等待本批次任务完成
                batch_results = await asyncio.gather(*tasks)
                
                # 过滤掉None结果
                valid_results = [result for result in batch_results if result]
                results.extend(valid_results)
                
        return results
        
    async def _handle_batch_wait(self, use_single_thread, skip_network_requests):
        """处理批次间等待
        
        Args:
            use_single_thread: 是否使用单线程模式
            skip_network_requests: 是否跳过网络请求
        """
        # 如果已经跳过网络请求了，则不需要等待
        if skip_network_requests:
            # 直接进行下一批处理，不等待
            logger.info(f"跳过网络请求模式：直接处理下一批视频...(当前429错误计数: {self.rate_limit_count})")
            return
            
        # 根据情况确定等待时间
        if self.rate_limit_count > 5:
            wait_time = 6.0  # 固定为6秒
        elif use_single_thread:
            wait_time = 1.0  # 单线程模式下批次间等待1秒
        else:
            wait_time = self.min_wait_time
            
        logger.info(f"等待 {wait_time} 秒后处理下一批...(当前429错误计数: {self.rate_limit_count})")
        await asyncio.sleep(wait_time) 