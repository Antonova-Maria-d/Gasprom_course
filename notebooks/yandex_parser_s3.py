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
    """ФИНАЛЬНАЯ ПРОДУКЦИОННАЯ версия - все поля для обучения модели"""
    
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
                print(f" Создан bucket: {self.s3_bucket}")
        except S3Error as e:
            print(f" Ошибка S3: {e}")
            self.upload_to_s3 = False
    
    def _make_request(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(1, 2))
                response = self.session.get(url, timeout=30)
                response.raise_for_status()
                response.encoding = 'utf-8'
                
                if "captcha" in response.text.lower() or "проверка" in response.text.lower():
                    print(f"   Капча, ждём 10 сек...")
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
                print(f"       [{img_index}/{total}] ")
            return s3_uri
            
        except Exception as e:
            if img_index <= 3:
                print(f"       [{img_index}/{total}] ")
            return None
    
    def get_listings(self, city: str = "moskva", pages: int = 2, max_offers: int = 100) -> List[str]:
        listing_urls = []
        
        print(f"\n СБОР ССЫЛОК")
        print(f" Город: {city}")
        print(f" Страниц: {pages}")
        print(f" Цель: {max_offers}\n")
        
        for page in range(1, pages + 1):
            if len(listing_urls) >= max_offers:
                break
            
            try:
                url = f"https://realty.yandex.ru/{city}/kupit/kvartira/" + (f"?page={page}" if page > 1 else "")
                print(f" Страница {page}...", end=" ")
                
                response = self._make_request(url)
                if not response:
                    print("нет")
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
                print(f" +{len(links)} (всего {len(listing_urls)})")
                
                time.sleep(random.uniform(1, 2))
            except Exception as e:
                print(f" Ошибка: {e}")
        
        listing_urls = list(set(listing_urls))[:max_offers]
        print(f"\n Собрано {len(listing_urls)} объявлений\n")
        return listing_urls
    
    def extract_full_description(self, soup: BeautifulSoup) -> str:
        """Извлечение ПОЛНОГО описания продавца"""
        description = ""
        
        # 1. Главный блок с полным описанием
        desc_elem = soup.find('div', class_='OfferCardTextDescription__text')
        if desc_elem:
            for hidden in desc_elem.find_all('span', class_=re.compile(r'isHidden|isEllipsis')):
                hidden.decompose()
            description = desc_elem.get_text(separator=' ', strip=True)
        
        # 2. Блок ExpandableData__root
        if not description or len(description) < 200:
            expandable = soup.find('div', class_='ExpandableData__root')
            if expandable:
                description = expandable.get_text(separator=' ', strip=True)
        
        # 3. Блок data-testid
        if not description or len(description) < 200:
            desc_elem = soup.find('div', {'data-testid': 'offer-description'})
            if desc_elem:
                description = desc_elem.get_text(separator=' ', strip=True)
        
        # 4. Любой длинный текст
        if not description or len(description) < 200:
            all_divs = soup.find_all('div')
            for div in all_divs:
                text = div.get_text(separator=' ', strip=True)
                if len(text) > 300 and ('квартир' in text or 'этаж' in text or 'м²' in text):
                    description = text
                    break
        
        if description:
            description = re.sub(r'\s+', ' ', description)
            description = description.replace('&nbsp;', ' ').replace('&amp;', '&')
            description = re.sub(r'Читать далее\s*', '', description)
            description = re.sub(r'Скрыть\s*', '', description)
            description = description.strip()
        
        return description
    
    def extract_house_type(self, soup: BeautifulSoup, html: str) -> Optional[str]:
        """Извлечение типа дома"""
        house_type = None
        
        # 1. Из JSON
        for script in soup.find_all('script'):
            if script.string:
                match = re.search(r'"houseType":\s*"([^"]+)"', script.string)
                if match:
                    house_type = match.group(1)
                    type_map = {
                        'HOUSE_TYPE_MONOLITHIC': 'монолитный',
                        'HOUSE_TYPE_BRICK': 'кирпичный',
                        'HOUSE_TYPE_PANEL': 'панельный',
                        'HOUSE_TYPE_BLOCK': 'блочный',
                        'HOUSE_TYPE_WOODEN': 'деревянный',
                        'HOUSE_TYPE_MONOLITHIC_BRICK': 'монолитно-кирпичный',
                        'HOUSE_TYPE_MONOLITH': 'монолитный',
                    }
                    house_type = type_map.get(house_type, house_type.lower().replace('_', ' '))
                    return house_type
        
        # 2. Из текста
        if not house_type:
            type_keywords = {
                'монолитный': 'монолитный',
                'кирпичный': 'кирпичный',
                'панельный': 'панельный',
                'блочный': 'блочный',
                'деревянный': 'деревянный',
                'монолитно-кирпичный': 'монолитно-кирпичный',
            }
            for keyword, value in type_keywords.items():
                if keyword in html.lower():
                    house_type = value
                    break
        
        return house_type
    
    def extract_renovation(self, soup: BeautifulSoup, html: str) -> Optional[str]:
        """Извлечение типа ремонта"""
        renovation = None
        
        # 1. Из JSON
        for script in soup.find_all('script'):
            if script.string:
                match = re.search(r'"renovation":\s*"([^"]+)"', script.string)
                if match:
                    renovation = match.group(1)
                    renovation_map = {
                        'RENOVATION_TYPE_WITHOUT': 'без отделки',
                        'RENOVATION_TYPE_CLEAN': 'чистовая отделка',
                        'RENOVATION_TYPE_COSMETIC': 'косметический ремонт',
                        'RENOVATION_TYPE_DESIGN': 'дизайнерский ремонт',
                        'RENOVATION_TYPE_EURO': 'евроремонт',
                        'RENOVATION_TYPE_PREMIUM': 'премиум ремонт',
                        'RENOVATION_TYPE_FINISHING': 'черновая отделка',
                        'RENOVATION_TYPE_ROUGH': 'черновая отделка',
                    }
                    renovation = renovation_map.get(renovation, renovation.lower().replace('_', ' '))
                    return renovation
        
        # 2. Из текста
        if not renovation:
            renovation_keywords = {
                'без отделки': 'без отделки',
                'чистовая отделка': 'чистовая отделка',
                'косметический ремонт': 'косметический ремонт',
                'дизайнерский ремонт': 'дизайнерский ремонт',
                'евроремонт': 'евроремонт',
                'под ключ': 'под ключ',
                'черновая отделка': 'черновая отделка',
            }
            for keyword, value in renovation_keywords.items():
                if keyword in html.lower():
                    renovation = value
                    break
        
        return renovation
    
    def extract_build_year(self, soup: BeautifulSoup, html: str) -> Optional[int]:
        """Извлечение года постройки"""
        year = None
        
        for script in soup.find_all('script'):
            if script.string:
                match = re.search(r'"buildYear":\s*(\d{4})', script.string)
                if match:
                    year = int(match.group(1))
                    return year
        
        patterns = [
            r'(\d{4})\s*года?\s*постройк[иа]',
            r'Год постройки[:\s]+(\d{4})',
            r'постро[еи]н\s+в\s+(\d{4})',
            r'сдан\s+в\s+(\d{4})',
            r'(\d{4})\s*год',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                year = int(match.group(1))
                return year
        
        return None
    
    def extract_bathroom(self, soup: BeautifulSoup, html: str) -> Optional[str]:
        """Извлечение типа санузла"""
        bathroom = None
        
        for script in soup.find_all('script'):
            if script.string:
                match = re.search(r'"bathroom":\s*"([^"]+)"', script.string)
                if match:
                    value = match.group(1)
                    bathroom_map = {
                        'BATHROOM_TYPE_MATCHED': 'совмещенный',
                        'BATHROOM_TYPE_SEPARATE': 'раздельный',
                        'BATHROOM_TYPE_MULTIPLE': 'раздельный (3 и более)',
                        'BATHROOM_TYPE_SEPARATED': 'раздельный',
                        'BATHROOM_TYPE_TWO_AND_MORE': 'раздельный (2 и более)',
                        'bathroom_type_two_and_more': 'раздельный (2 и более)',
                        'bathroom_type_combined': 'совмещенный',
                        'bathroom_type_separate': 'раздельный',
                    }
                    bathroom = bathroom_map.get(value, value)
                    if bathroom and bathroom != value:
                        return bathroom
        
        if not bathroom:
            if 'совмещен' in html.lower():
                bathroom = 'совмещенный'
            elif 'раздельн' in html.lower():
                bathroom = 'раздельный'
        
        return bathroom
    
    def extract_areas_from_page(self, soup: BeautifulSoup, html: str, description: str) -> Dict:
        """Извлечение площадей из всех возможных источников"""
        areas = {}
        
        # 1. Из data-testid
        area_elem = soup.find('span', {'data-testid': 'total-area'})
        if area_elem:
            area_text = area_elem.get_text(strip=True)
            area_match = re.search(r'([\d.,]+)', area_text)
            if area_match:
                areas['total_area'] = float(area_match.group(1).replace(',', '.'))
        
        living_elem = soup.find('span', {'data-testid': 'living-area'})
        if living_elem:
            living_text = living_elem.get_text(strip=True)
            living_match = re.search(r'([\d.,]+)', living_text)
            if living_match:
                areas['area_living'] = float(living_match.group(1).replace(',', '.'))
        
        kitchen_elem = soup.find('span', {'data-testid': 'kitchen-area'})
        if kitchen_elem:
            kitchen_text = kitchen_elem.get_text(strip=True)
            kitchen_match = re.search(r'([\d.,]+)', kitchen_text)
            if kitchen_match:
                areas['area_kitchen'] = float(kitchen_match.group(1).replace(',', '.'))
        
        # 2. Из JSON
        if 'total_area' not in areas:
            for script in soup.find_all('script'):
                if script.string:
                    match = re.search(r'"totalArea":\s*([\d.]+)', script.string)
                    if match:
                        areas['total_area'] = float(match.group(1))
                        break
        
        # 3. Из текста описания
        text_to_search = description + " " + html
        
        if 'total_area' not in areas:
            patterns = [
                r'Общая площадь\s+([\d.,]+)\s*м²',
                r'общей площадью\s+([\d.,]+)\s*м²',
                r'площадь\s+([\d.,]+)\s*м²',
                r'([\d.,]+)\s*м²\s*общая',
            ]
            for pattern in patterns:
                match = re.search(pattern, text_to_search, re.IGNORECASE)
                if match:
                    areas['total_area'] = float(match.group(1).replace(',', '.'))
                    break
        
        if 'area_living' not in areas:
            patterns = [
                r'Жилая площадь\s+([\d.,]+)\s*м²',
                r'жилая\s+([\d.,]+)\s*м²',
                r'([\d.,]+)\s*м²\s*жилая',
            ]
            for pattern in patterns:
                match = re.search(pattern, text_to_search, re.IGNORECASE)
                if match:
                    areas['area_living'] = float(match.group(1).replace(',', '.'))
                    break
        
        if 'area_kitchen' not in areas:
            patterns = [
                r'Площадь кухни\s+([\d.,]+)\s*м²',
                r'кухня\s+([\d.,]+)\s*м²',
                r'кухни\s+([\d.,]+)\s*м²',
            ]
            for pattern in patterns:
                match = re.search(pattern, text_to_search, re.IGNORECASE)
                if match:
                    areas['area_kitchen'] = float(match.group(1).replace(',', '.'))
                    break
        
        return areas
    
    def scrape_listing(self, url: str) -> Optional[Dict]:
        response = self._make_request(url)
        if not response:
            return None
        
        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        offer_id = url.rstrip('/').split('/')[-1]
        
        print(f"     Парсинг...")
        
        data = {}
        
        # ==================== 1. ЦЕНА ====================
        price_elem = soup.find('span', {'data-testid': 'price-value'})
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            price_match = re.search(r'(\d[\d\s]*\d)', price_text)
            if price_match:
                data['price'] = int(re.sub(r'[\s]', '', price_match.group(1)))
        
        if 'price' not in data:
            for script in soup.find_all('script'):
                if script.string:
                    match = re.search(r'"price":\s*(\d+)', script.string)
                    if match:
                        data['price'] = int(match.group(1))
                        break
        
        # ==================== 2. ПОЛНОЕ ОПИСАНИЕ ====================
        full_description = self.extract_full_description(soup)
        if full_description:
            data['description'] = full_description
        
        # ==================== 3. ПЛОЩАДИ ====================
        areas = self.extract_areas_from_page(soup, html, data.get('description', ''))
        data.update(areas)
        
        # ==================== 4. ЭТАЖИ ====================
        floor_elem = soup.find('span', {'data-testid': 'floor'})
        if floor_elem:
            floor_text = floor_elem.get_text(strip=True)
            floors_match = re.search(r'(\d+)\s*из\s*(\d+)', floor_text)
            if floors_match:
                data['floor'] = int(floors_match.group(1))
                data['total_floors'] = int(floors_match.group(2))
        
        if 'floor' not in data:
            floor_match = re.search(r'на\s+(\d+)\s+этаж[еи]\s+из\s+(\d+)', html, re.IGNORECASE)
            if floor_match:
                data['floor'] = int(floor_match.group(1))
                data['total_floors'] = int(floor_match.group(2))
        
        # ==================== 5. КОМНАТЫ ====================
        rooms_elem = soup.find('span', {'data-testid': 'rooms'})
        if rooms_elem:
            rooms_text = rooms_elem.get_text(strip=True).lower()
            if 'студия' in rooms_text:
                data['rooms'] = 'студия'
            else:
                rooms_match = re.search(r'(\d+)', rooms_text)
                if rooms_match:
                    data['rooms'] = int(rooms_match.group(1))
        
        if 'rooms' not in data:
            title_elem = soup.find('h1')
            if title_elem:
                title = title_elem.get_text(strip=True)
                rooms_match = re.search(r'(\d+)-комнат', title)
                if rooms_match:
                    data['rooms'] = int(rooms_match.group(1))
                elif 'студия' in title.lower():
                    data['rooms'] = 'студия'
        
        # ==================== 6. АДРЕС ====================
        address_elem = soup.find('span', {'data-testid': 'address'})
        if address_elem:
            data['address'] = address_elem.get_text(strip=True)
        
        if 'address' not in data:
            address_match = re.search(r'📍\s*Адрес:\s*([^.]+)', html)
            if address_match:
                data['address'] = address_match.group(1).strip()
        
        # ==================== 7. ГОД ПОСТРОЙКИ ====================
        build_year = self.extract_build_year(soup, html)
        if build_year:
            data['build_year'] = build_year
        
        # ==================== 8. ВЫСОТА ПОТОЛКОВ ====================
        height_match = re.search(r'высот[ае]\s+потолк[ао]в?\s+([\d.,]+)\s*м', html, re.IGNORECASE)
        if height_match:
            data['ceiling_height'] = float(height_match.group(1).replace(',', '.'))
        
        # ==================== 9. САНУЗЕЛ ====================
        bathroom = self.extract_bathroom(soup, html)
        if bathroom:
            data['bathroom'] = bathroom
        
        # ==================== 10. ТИП ДОМА ====================
        house_type = self.extract_house_type(soup, html)
        if house_type:
            data['house_type'] = house_type
        
        # ==================== 11. РЕМОНТ ====================
        renovation = self.extract_renovation(soup, html)
        if renovation:
            data['renovation'] = renovation
        
        # ==================== 12. ЗАГОЛОВОК ====================
        title_elem = soup.find('h1')
        if title_elem:
            data['title'] = title_elem.get_text(strip=True)
        
        # ==================== 13. БАЗОВЫЕ ПОЛЯ ====================
        data['url'] = url
        data['offer_id'] = offer_id
        data['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # ==================== 14. ИЗОБРАЖЕНИЯ ====================
        image_urls = set()
        
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
        
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src and 'blob:' not in src:
                if src.startswith('//'):
                    src = 'https:' + src
                if src.startswith('http'):
                    src = src.split('?')[0]
                    if 'get-icon' not in src and 'logo' not in src:
                        image_urls.add(src)
        
        final_urls = []
        skip_patterns = ['thumb', 'preview', 'small', 'icon', 'logo', 'get-icon']
        for url_img in image_urls:
            if not any(pattern in url_img.lower() for pattern in skip_patterns):
                final_urls.append(url_img)
        
        final_urls = list(dict.fromkeys(final_urls))[:15]
        
        if final_urls:
            print(f"     Найдено изображений: {len(final_urls)}")
            s3_uris = []
            print(f"     Загрузка в S3...")
            for idx, img_url in enumerate(final_urls, 1):
                s3_uri = self.process_image(img_url, offer_id, idx, len(final_urls))
                if s3_uri:
                    s3_uris.append(s3_uri)
                time.sleep(0.2)
            data['s3_uris'] = s3_uris if s3_uris else None
            data['s3_uris_count'] = len(s3_uris) if s3_uris else 0
        
        data['images_count'] = len(final_urls)
        
        # Выводим результат
        print(f"     ИТОГО: цена={data.get('price', '❌')}, площадь={data.get('total_area', '❌')}, жилая={data.get('area_living', '❌')}, кухня={data.get('area_kitchen', '❌')}, этаж={data.get('floor', '❌')}/{data.get('total_floors', '❌')}, год={data.get('build_year', '❌')}, тип дома={data.get('house_type', '❌')}, ремонт={data.get('renovation', '❌')}, описание={len(data.get('description', ''))} символов")
        
        return data
    
    def save_to_csv(self, data: List[Dict], filename: str = None):
        if not data:
            print(" Нет данных")
            return
        
        if not filename:
            filename = f"realty_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        fieldnames = [
            'timestamp', 'offer_id', 'title', 'price', 'total_area',
            'area_living', 'area_kitchen', 'rooms', 'floor', 'total_floors',
            'address', 'description', 'ceiling_height', 'build_year', 
            'house_type', 'bathroom', 'renovation', 'url', 
            'images_count', 's3_uris_count', 's3_uris'
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
                    if field == 'description' and isinstance(value, str):
                        value = value.replace('\n', ' ').replace('\r', ' ').strip()
                    if isinstance(value, str):
                        value = ' '.join(value.split())
                    row[field] = value
                writer.writerow(row)
        
        print(f"\n СОХРАНЕНО")
        print(f" Файл: {filename}")
        print(f" Записей: {len(data)}")
        
        if self.upload_to_s3:
            total_uploads = sum(item.get('s3_uris_count', 0) for item in data)
            print(f"  Фото в S3: {total_uploads}")
    
    def print_statistics(self, data: List[Dict]):
        if not data:
            return
        
        total = len(data)
        
        print("\n" + "="*60)
        print(" СТАТИСТИКА ДЛЯ ОБУЧЕНИЯ МОДЕЛИ")
        print("="*60)
        print(f" Всего объявлений: {total}")
        print(f" Цена: {sum(1 for x in data if x.get('price'))}/{total}")
        print(f" Общая площадь: {sum(1 for x in data if x.get('total_area'))}/{total}")
        print(f" Жилая площадь: {sum(1 for x in data if x.get('area_living'))}/{total}")
        print(f" Площадь кухни: {sum(1 for x in data if x.get('area_kitchen'))}/{total}")
        print(f" Номер этажа: {sum(1 for x in data if x.get('floor'))}/{total}")
        print(f" Адрес: {sum(1 for x in data if x.get('address'))}/{total}")
        print(f" Полное описание (>200 символов): {sum(1 for x in data if x.get('description') and len(x.get('description', '')) > 200)}/{total}")
        print(f" Год постройки: {sum(1 for x in data if x.get('build_year'))}/{total}")
        print(f" Высота потолков: {sum(1 for x in data if x.get('ceiling_height'))}/{total}")
        print(f" Тип дома: {sum(1 for x in data if x.get('house_type'))}/{total}")
        print(f" Санузел: {sum(1 for x in data if x.get('bathroom'))}/{total}")
        print(f" Ремонт: {sum(1 for x in data if x.get('renovation'))}/{total}")
        print(f"  Фото: {sum(1 for x in data if x.get('s3_uris_count', 0) > 0)}/{total}")
        
        total_photos = sum(x.get('s3_uris_count', 0) for x in data)
        print(f" Всего фото в S3: {total_photos}")
        



def main():
    print("="*60)
    print(" ФИНАЛЬНЫЙ ПАРСЕР - ВСЕ ПОЛЯ ДЛЯ ОБУЧЕНИЯ МОДЕЛИ")
    print("="*60)
    
    CITY = "moskva"
    PAGES = 23
    MAX_OFFERS = 800
    USE_S3 = True
    
    print(f"\n НАСТРОЙКИ:")
    print(f"   Город: {CITY}")
    print(f"   Страниц: {PAGES}")
    print(f"   Макс. объявлений: {MAX_OFFERS}")
    print(f"   S3 загрузка: {' ВКЛ' if USE_S3 else ' ВЫКЛ'}")
    
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
            print(" Нет объявлений")
            return
        
        print(" НАЧИНАЕМ ПАРСИНГ")
        print("="*60)
        
        results = []
        for i, url in enumerate(urls, 1):
            print(f"\n[{i}/{len(urls)}] {url.split('/')[-2]}...")
            
            data = scraper.scrape_listing(url)
            
            if data:
                results.append(data)
                print(f"    {data.get('title', 'N/A')[:50]}")
                print(f"    Цена: {data.get('price', 'N/A'):,} ₽" if data.get('price') else "   💰 Цена: N/A")
                print(f"    Общая площадь: {data.get('total_area', 'N/A')} м²")
                print(f"    Жилая: {data.get('area_living', 'N/A')} м²")
                print(f"    Кухня: {data.get('area_kitchen', 'N/A')} м²")
                print(f"    Этаж: {data.get('floor', 'N/A')}/{data.get('total_floors', 'N/A')}")
                print(f"    Год: {data.get('build_year', 'N/A')}")
                print(f"    Тип дома: {data.get('house_type', 'N/A')}")
                print(f"    Санузел: {data.get('bathroom', 'N/A')}")
                print(f"    Ремонт: {data.get('renovation', 'N/A')}")
                print(f"    Описание: {len(data.get('description', ''))} символов")
                print(f"     Фото в S3: {data.get('s3_uris_count', 0)}")
            else:
                print(f"    Не удалось собрать данные")
            
            time.sleep(random.uniform(1, 2))
        
        if results:
            scraper.save_to_csv(results)
            scraper.print_statistics(results)
            print("\n ПАРСИНГ ЗАВЕРШЕН!")
        else:
            print("\n Не удалось собрать данные")
            
    except KeyboardInterrupt:
        print("\n\n Прервано пользователем")
    except Exception as e:
        print(f"\n Ошибка: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()