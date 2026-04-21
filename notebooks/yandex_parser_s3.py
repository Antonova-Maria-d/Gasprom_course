import requests
from bs4 import BeautifulSoup
import json
import time
import csv
import re
from datetime import datetime
from typing import Dict, List, Optional
import random
from urllib.parse import urljoin
from pathlib import Path
from io import BytesIO
from minio import Minio
from minio.error import S3Error

class YandexRealtyScraperS3:
    """Финальная версия парсера - все данные в правильные колонки"""
    
    def __init__(self, 
                 upload_to_s3=False,
                 s3_endpoint="localhost:9000",
                 s3_access_key="minioadmin",
                 s3_secret_key="minioadmin123",
                 s3_bucket="realty-images",
                 s3_secure=False):
        
        self.upload_to_s3 = upload_to_s3
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Charset': 'utf-8',
        })
        
        if self.upload_to_s3:
            self.s3_client = Minio(
                s3_endpoint,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
                secure=s3_secure
            )
            self.s3_bucket = s3_bucket
            self._ensure_bucket_exists()
    
    def _ensure_bucket_exists(self):
        try:
            if not self.s3_client.bucket_exists(self.s3_bucket):
                self.s3_client.make_bucket(self.s3_bucket)
                print(f"✅ Создан bucket: {self.s3_bucket}")
        except S3Error as e:
            print(f"❌ Ошибка S3: {e}")
            self.upload_to_s3 = False
    
    def _make_request(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(1, 2))
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
                
                if "captcha" in response.text.lower() or "проверка" in response.text.lower():
                    print(f"  ⚠️ Капча, ждём 10 сек...")
                    time.sleep(10)
                    continue
                    
                return response
            except Exception as e:
                if attempt == max_retries - 1:
                    return None
                time.sleep(2 ** attempt)
        return None
    
    def upload_image_to_s3(self, image_data: bytes, offer_id: str, img_index: int, content_type: str) -> Optional[str]:
        if not self.upload_to_s3:
            return None
        
        try:
            extension = 'jpg'
            if 'png' in content_type:
                extension = 'png'
            elif 'webp' in content_type:
                extension = 'webp'
            
            object_name = f"offers/{offer_id}/{img_index:02d}.{extension}"
            image_stream = BytesIO(image_data)
            self.s3_client.put_object(
                bucket_name=self.s3_bucket,
                object_name=object_name,
                data=image_stream,
                length=len(image_data),
                content_type=content_type
            )
            return f"s3://{self.s3_bucket}/{object_name}"
        except Exception as e:
            return None
    
    def process_image(self, img_url: str, offer_id: str, img_index: int, total: int) -> Optional[str]:
        try:
            if img_url.startswith('//'):
                img_url = 'https:' + img_url
            
            img_url = img_url.split('?')[0]
            
            headers = {'Referer': 'https://realty.yandex.ru/'}
            response = self.session.get(img_url, headers=headers, timeout=15)
            response.raise_for_status()
            
            content_type = response.headers.get('content-type', 'image/jpeg')
            s3_uri = self.upload_image_to_s3(response.content, offer_id, img_index, content_type)
            
            if s3_uri and (img_index <= 5 or img_index == total):
                print(f"       [{img_index}/{total}] ✅")
            return s3_uri
            
        except Exception as e:
            if img_index <= 3:
                print(f"       [{img_index}/{total}] ❌")
            return None
    
    def get_listings(self, city: str = "moskva", pages: int = 2, max_offers: int = 100) -> List[str]:
        listing_urls = []
        
        print(f"\n🔍 СБОР ССЫЛОК")
        print(f"📍 Город: {city}")
        print(f"📄 Страниц: {pages}")
        print(f"🎯 Цель: {max_offers}\n")
        
        for page in range(1, pages + 1):
            if len(listing_urls) >= max_offers:
                break
            
            try:
                url = f"https://realty.yandex.ru/{city}/kupit/kvartira/" + (f"?page={page}" if page > 1 else "")
                print(f"📄 Страница {page}...", end=" ")
                
                response = self._make_request(url)
                if not response:
                    print("❌")
                    continue
                
                soup = BeautifulSoup(response.content, 'html.parser')
                links = set()
                
                for script in soup.find_all('script'):
                    if script.string:
                        matches = re.findall(r'/offer/(\d+)', script.string)
                        for oid in matches:
                            links.add(f'https://realty.yandex.ru/offer/{oid}/')
                
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if '/offer/' in href:
                        if href.startswith('/'):
                            links.add('https://realty.yandex.ru' + href)
                        elif 'realty.yandex.ru' in href:
                            links.add(href)
                
                links = list(links)
                listing_urls.extend(links)
                print(f"✅ +{len(links)} (всего {len(listing_urls)})")
                
                time.sleep(random.uniform(1, 2))
            except Exception as e:
                print(f"❌ Ошибка: {e}")
        
        listing_urls = list(set(listing_urls))[:max_offers]
        print(f"\n✅ Собрано {len(listing_urls)} объявлений\n")
        return listing_urls
    
    def extract_from_next_data(self, soup: BeautifulSoup) -> Dict:
        """Извлечение данных из __NEXT_DATA__"""
        data = {}
        
        next_data_script = soup.find('script', id='__NEXT_DATA__')
        if next_data_script and next_data_script.string:
            try:
                next_data = json.loads(next_data_script.string)
                
                def find_offer_data(obj, depth=0):
                    if depth > 15:
                        return
                    
                    if isinstance(obj, dict):
                        if 'offer' in obj:
                            offer = obj['offer']
                            if isinstance(offer, dict):
                                # Адрес
                                if 'address' in offer and 'address' not in data:
                                    data['address'] = offer['address']
                                
                                # Полное описание
                                if 'description' in offer and 'description' not in data:
                                    data['description'] = offer['description']
                                
                                # Цена
                                if 'price' in offer and 'price' not in data:
                                    try:
                                        data['price'] = int(offer['price'])
                                    except:
                                        pass
                                
                                # Площади
                                if 'area' in offer and 'total_area' not in data:
                                    try:
                                        data['total_area'] = float(offer['area'])
                                    except:
                                        pass
                                
                                if 'livingArea' in offer and 'area_living' not in data:
                                    try:
                                        data['area_living'] = float(offer['livingArea'])
                                    except:
                                        pass
                                
                                if 'kitchenArea' in offer and 'area_kitchen' not in data:
                                    try:
                                        data['area_kitchen'] = float(offer['kitchenArea'])
                                    except:
                                        pass
                                
                                # Этажи (ВАЖНО!)
                                if 'floor' in offer and 'floor' not in data:
                                    try:
                                        data['floor'] = int(offer['floor'])
                                    except:
                                        pass
                                
                                if 'floorsTotal' in offer and 'total_floors' not in data:
                                    try:
                                        data['total_floors'] = int(offer['floorsTotal'])
                                    except:
                                        pass
                                
                                # Комнаты
                                if 'rooms' in offer and 'rooms' not in data:
                                    try:
                                        if offer['rooms'] == 'studio':
                                            data['rooms'] = 'студия'
                                        else:
                                            data['rooms'] = int(offer['rooms'])
                                    except:
                                        pass
                                
                                # Год постройки
                                if 'buildYear' in offer and 'build_year' not in data:
                                    try:
                                        data['build_year'] = int(offer['buildYear'])
                                    except:
                                        pass
                                
                                # Высота потолков
                                if 'ceilingHeight' in offer and 'ceiling_height' not in data:
                                    try:
                                        data['ceiling_height'] = float(offer['ceilingHeight'])
                                    except:
                                        pass
                                
                                # Тип дома
                                if 'houseType' in offer and 'house_type' not in data:
                                    data['house_type'] = offer['houseType']
                                
                                # Санузел
                                if 'bathroom' in offer and 'bathroom' not in data:
                                    data['bathroom'] = offer['bathroom']
                                
                                # Ремонт
                                if 'renovation' in offer and 'renovation' not in data:
                                    data['renovation'] = offer['renovation']
                                
                                # Координаты
                                if 'geo' in offer:
                                    geo = offer['geo']
                                    if 'latitude' in geo and 'latitude' not in data:
                                        try:
                                            data['latitude'] = float(geo['latitude'])
                                        except:
                                            pass
                                    if 'longitude' in geo and 'longitude' not in data:
                                        try:
                                            data['longitude'] = float(geo['longitude'])
                                        except:
                                            pass
                        
                        for value in obj.values():
                            find_offer_data(value, depth + 1)
                    
                    elif isinstance(obj, list):
                        for item in obj:
                            find_offer_data(item, depth + 1)
                
                find_offer_data(next_data)
                
            except Exception as e:
                pass
        
        return data
    
    def extract_full_description(self, soup: BeautifulSoup) -> str:
        """Извлечение ПОЛНОГО описания из блока offer-description"""
        # Ищем блок с полным описанием
        desc_selectors = [
            ('div', {'data-testid': 'offer-description'}),
            ('div', {'class': 'OfferDescription'}),
            ('div', {'itemprop': 'description'}),
            ('section', {'data-testid': 'description-section'}),
        ]
        
        for tag, attrs in desc_selectors:
            elem = soup.find(tag, attrs)
            if elem:
                # Получаем весь текст, включая вложенные элементы
                text = elem.get_text(separator='\n', strip=True)
                if len(text) > 100:  # Настоящее описание обычно длинное
                    return text
        
        return ""
    
    def extract_from_parameters_table(self, soup: BeautifulSoup) -> Dict:
        """Извлечение параметров из таблицы характеристик"""
        data = {}
        
        # Ищем таблицу с параметрами
        param_selectors = [
            ('div', {'data-testid': 'parameters'}),
            ('div', {'class': 'OfferParameters'}),
            ('table', {'class': 'parameters'}),
        ]
        
        for tag, attrs in param_selectors:
            container = soup.find(tag, attrs)
            if container:
                # Ищем все строки с параметрами
                rows = container.find_all(['div', 'tr'], class_=re.compile(r'row|item|param', re.I))
                
                for row in rows:
                    # Ищем название параметра и значение
                    label_elem = row.find(['span', 'div', 'dt'], class_=re.compile(r'label|name|title', re.I))
                    value_elem = row.find(['span', 'div', 'dd'], class_=re.compile(r'value|description', re.I))
                    
                    if label_elem and value_elem:
                        label = label_elem.get_text(strip=True).lower()
                        value = value_elem.get_text(strip=True)
                        
                        # Год постройки
                        if ('год' in label or 'постройк' in label) and 'build_year' not in data:
                            match = re.search(r'(\d{4})', value)
                            if match:
                                data['build_year'] = int(match.group(1))
                        
                        # Санузел
                        elif 'санузел' in label and 'bathroom' not in data:
                            data['bathroom'] = value
                        
                        # Ремонт
                        elif 'ремонт' in label and 'renovation' not in data:
                            data['renovation'] = value
                        
                        # Тип дома
                        elif ('тип' in label and 'дом' in label) and 'house_type' not in data:
                            data['house_type'] = value
                        
                        # Высота потолков
                        elif ('высота' in label and 'потолк' in label) and 'ceiling_height' not in data:
                            match = re.search(r'([\d.,]+)', value)
                            if match:
                                data['ceiling_height'] = float(match.group(1).replace(',', '.'))
        
        return data
    
    def parse_images(self, soup: BeautifulSoup, html: str, offer_id: str) -> tuple:
        """Парсинг всех изображений с загрузкой в S3"""
        image_urls = set()
        
        # JSON-LD
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                json_data = json.loads(script.string)
                if isinstance(json_data, dict) and 'image' in json_data:
                    images = json_data['image']
                    if isinstance(images, list):
                        for img in images:
                            if isinstance(img, str):
                                image_urls.add(img)
                            elif isinstance(img, dict) and 'url' in img:
                                image_urls.add(img['url'])
                    elif isinstance(images, str):
                        image_urls.add(images)
            except:
                pass
        
        # img теги
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src and 'blob:' not in src:
                if src.startswith('//'):
                    src = 'https:' + src
                if src.startswith('http'):
                    src = src.split('?')[0]
                    if 'get-icon' not in src and 'logo' not in src:
                        image_urls.add(src)
        
        # Регулярные выражения
        img_pattern = r'https?://avatars\.mds\.yandex\.net/get-[^"\']+\.(jpg|jpeg|png|webp)[^"\']*'
        matches = re.findall(img_pattern, html, re.IGNORECASE)
        for match in matches:
            if isinstance(match, tuple):
                match = match[0]
            if match.startswith('//'):
                match = 'https:' + match
            if match.startswith('http'):
                match = match.split('?')[0]
                image_urls.add(match)
        
        # Фильтрация
        final_urls = []
        skip_patterns = ['thumb', 'preview', 'small', 'icon', 'logo', 'get-icon']
        for url in image_urls:
            if not any(pattern in url.lower() for pattern in skip_patterns):
                final_urls.append(url)
        
        final_urls = list(dict.fromkeys(final_urls))[:15]  # Увеличил до 15 фото
        
        if final_urls:
            print(f"    📸 Найдено изображений: {len(final_urls)}")
            
            s3_uris = []
            print(f"    💾 Загрузка в S3...")
            for idx, img_url in enumerate(final_urls, 1):
                s3_uri = self.process_image(img_url, offer_id, idx, len(final_urls))
                if s3_uri:
                    s3_uris.append(s3_uri)
                time.sleep(0.2)
            
            return final_urls, s3_uris
        
        return [], []
    
    def scrape_listing(self, url: str) -> Optional[Dict]:
        response = self._make_request(url)
        if not response:
            return None
        
        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        offer_id = url.rstrip('/').split('/')[-1]
        
        print(f"    📋 Парсинг...")
        
        # Собираем данные из всех источников
        data = {}
        
        # 1. Из __NEXT_DATA__ (основные данные)
        data.update(self.extract_from_next_data(soup))
        
        # 2. Из таблицы параметров (дополнительные данные)
        data.update(self.extract_from_parameters_table(soup))
        
        # 3. Базовые поля
        data['url'] = url
        data['offer_id'] = offer_id
        data['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Заголовок
        title_elem = soup.find('h1')
        if title_elem:
            title = title_elem.get_text(strip=True)
            data['title'] = title
            
            # Если нет площади, пробуем извлечь из заголовка
            if 'total_area' not in data:
                area_match = re.search(r'(\d+[,.]?\d*)\s*м²', title)
                if area_match:
                    try:
                        data['total_area'] = float(area_match.group(1).replace(',', '.'))
                    except:
                        pass
            
            # Если нет комнат, пробуем извлечь из заголовка
            if 'rooms' not in data:
                rooms_match = re.search(r'(\d+)-комнат', title)
                if rooms_match:
                    try:
                        data['rooms'] = int(rooms_match.group(1))
                    except:
                        pass
                elif 'студия' in title.lower():
                    data['rooms'] = 'студия'
        
        # Полное описание (ВАЖНО! - берём из блока, а не из meta)
        full_description = self.extract_full_description(soup)
        if full_description:
            data['description'] = full_description
        elif 'description' not in data:
            # Если нет полного, пробуем meta
            meta_desc = soup.find('meta', {'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                data['description'] = meta_desc['content']
        
        # Адрес (если не нашли в next_data)
        if 'address' not in data:
            address_elem = soup.find('span', {'data-testid': 'address'})
            if address_elem:
                data['address'] = address_elem.get_text(strip=True)
        
        # Этажи (если не нашли в next_data, ищем в тексте)
        if 'floor' not in data or 'total_floors' not in data:
            floor_match = re.search(r'на\s+(\d+)\s+этаж[еи]\s+из\s+(\d+)', html, re.IGNORECASE)
            if floor_match:
                if 'floor' not in data:
                    data['floor'] = int(floor_match.group(1))
                if 'total_floors' not in data:
                    data['total_floors'] = int(floor_match.group(2))
        
        # Год постройки (если не нашли, ищем в тексте)
        if 'build_year' not in data:
            year_match = re.search(r'(\d{4})\s*года?\s*постройк[иа]', html, re.IGNORECASE)
            if not year_match:
                year_match = re.search(r'Год постройки[:\s]+(\d{4})', html)
            if year_match:
                data['build_year'] = int(year_match.group(1))
        
        # Изображения
        image_urls, s3_uris = self.parse_images(soup, html, offer_id)
        data['image_urls'] = image_urls
        data['images_count'] = len(image_urls)
        data['s3_uris'] = s3_uris if s3_uris else None
        data['s3_uris_count'] = len(s3_uris) if s3_uris else 0
        
        # Выводим результат для отладки
        print(f"    📊 Результат: площадь={data.get('total_area', '❌')}, этаж={data.get('floor', '❌')}/{data.get('total_floors', '❌')}, адрес={data.get('address', '❌')[:30] if data.get('address') else '❌'}, описание={'✅' if len(data.get('description', '')) > 100 else '❌'}, год={data.get('build_year', '❌')}")
        
        return data
    
    def save_to_csv(self, data: List[Dict], filename: str = None):
        if not data:
            print("❌ Нет данных")
            return
        
        if not filename:
            filename = f"realty_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        fieldnames = [
            'timestamp', 'offer_id', 'title', 'price', 'total_area',
            'area_living', 'area_kitchen', 'rooms', 'floor', 'total_floors',
            'address', 'latitude', 'longitude', 'description',
            'ceiling_height', 'build_year', 'house_type', 'bathroom', 'renovation',
            'url', 'images_count', 's3_uris_count', 's3_uris'
        ]
        
        with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';', extrasaction='ignore')
            writer.writeheader()
            
            for item in data:
                row = {}
                for field in fieldnames:
                    value = item.get(field, '')
                    if value is None:
                        value = ''
                    if field == 's3_uris' and isinstance(value, list):
                        value = ' | '.join(value)
                    if isinstance(value, str):
                        value = ' '.join(value.split())
                    row[field] = value
                writer.writerow(row)
        
        print(f"\n💾 СОХРАНЕНО")
        print(f"📁 Файл: {filename}")
        print(f"📊 Записей: {len(data)}")
        
        if self.upload_to_s3:
            total_uploads = sum(item.get('s3_uris_count', 0) for item in data)
            print(f"🖼️  Фото в S3: {total_uploads}")
    
    def print_statistics(self, data: List[Dict]):
        if not data:
            return
        
        total = len(data)
        
        print("\n" + "="*60)
        print("📊 СТАТИСТИКА")
        print("="*60)
        print(f"📝 Всего: {total}")
        print(f"💰 Цена: {sum(1 for x in data if x.get('price'))}/{total}")
        print(f"📏 Общая площадь: {sum(1 for x in data if x.get('total_area'))}/{total}")
        print(f"🍳 Площадь кухни: {sum(1 for x in data if x.get('area_kitchen'))}/{total}")
        print(f"🏢 Этаж: {sum(1 for x in data if x.get('floor'))}/{total}")
        print(f"📍 Адрес: {sum(1 for x in data if x.get('address'))}/{total}")
        print(f"📄 Полное описание: {sum(1 for x in data if x.get('description') and len(x.get('description', '')) > 100)}/{total}")
        print(f"📅 Год постройки: {sum(1 for x in data if x.get('build_year'))}/{total}")
        print(f"📐 Высота потолков: {sum(1 for x in data if x.get('ceiling_height'))}/{total}")
        print(f"🏠 Тип дома: {sum(1 for x in data if x.get('house_type'))}/{total}")
        print(f"🛁 Санузел: {sum(1 for x in data if x.get('bathroom'))}/{total}")
        print(f"🔨 Ремонт: {sum(1 for x in data if x.get('renovation'))}/{total}")
        print(f"🖼️  Фото: {sum(1 for x in data if x.get('s3_uris_count', 0) > 0)}/{total}")
        
        total_photos = sum(x.get('s3_uris_count', 0) for x in data)
        print(f"📸 Всего фото в S3: {total_photos}")
        
        if data:
            print("\n📋 ПРИМЕР ЗАПОЛНЕНИЯ (первое объявление):")
            sample = data[0]
            for field in ['total_area', 'floor', 'total_floors', 'address', 'build_year', 'bathroom', 'ceiling_height', 'rooms']:
                value = sample.get(field, '❌')
                if field == 'address' and value != '❌':
                    value = value[:50] + '...' if len(value) > 50 else value
                elif field == 'description' and value != '❌':
                    value = value[:80] + '...' if len(value) > 80 else value
                print(f"   {field}: {value}")
        print("="*60)


def main():
    print("="*60)
    print("🚀 ФИНАЛЬНАЯ ВЕРСИЯ ПАРСЕРА ЯНДЕКС.НЕДВИЖИМОСТИ")
    print("="*60)
    
    CITY = "moskva"
    PAGES = 2
    MAX_OFFERS = 10
    USE_S3 = True
    
    print(f"\n⚙️ НАСТРОЙКИ:")
    print(f"   Город: {CITY}")
    print(f"   Страниц: {PAGES}")
    print(f"   Макс. объявлений: {MAX_OFFERS}")
    print(f"   S3 загрузка: {'✅ ВКЛ' if USE_S3 else '❌ ВЫКЛ'}")
    
    scraper = YandexRealtyScraperS3(
        upload_to_s3=USE_S3,
        s3_endpoint="localhost:9000",
        s3_access_key="minioadmin",
        s3_secret_key="minioadmin123",
        s3_bucket="realty-images"
    )
    
    try:
        urls = scraper.get_listings(city=CITY, pages=PAGES, max_offers=MAX_OFFERS)
        
        if not urls:
            print("❌ Нет объявлений")
            return
        
        print("📋 НАЧИНАЕМ ПАРСИНГ")
        print("="*60)
        
        results = []
        for i, url in enumerate(urls, 1):
            print(f"\n[{i}/{len(urls)}] {url.split('/')[-2]}...")
            
            data = scraper.scrape_listing(url)
            
            if data:
                results.append(data)
                print(f"   ✅ {data.get('title', 'N/A')[:50]}")
                print(f"   💰 Цена: {data.get('price', 'N/A'):,} ₽" if data.get('price') else "   💰 Цена: N/A")
                print(f"   📏 Общая площадь: {data.get('total_area', 'N/A')} м²")
                print(f"   🏢 Этаж: {data.get('floor', 'N/A')}/{data.get('total_floors', 'N/A')}")
                print(f"   📍 Адрес: {data.get('address', 'N/A')[:40]}")
                print(f"   📅 Год постройки: {data.get('build_year', 'N/A')}")
                print(f"   🛁 Санузел: {data.get('bathroom', 'N/A')}")
                print(f"   📄 Описание: {data.get('description', 'N/A')[:80]}..." if data.get('description') else "   📄 Описание: N/A")
                print(f"   🖼️  Фото в S3: {data.get('s3_uris_count', 0)}")
            else:
                print(f"   ❌ Не удалось собрать данные")
            
            time.sleep(random.uniform(1, 2))
        
        if results:
            scraper.save_to_csv(results)
            scraper.print_statistics(results)
            print("\n🎉 ПАРСИНГ ЗАВЕРШЕН!")
        else:
            print("\n❌ Не удалось собрать данные")
            
    except KeyboardInterrupt:
        print("\n\n⚠️ Прервано пользователем")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()