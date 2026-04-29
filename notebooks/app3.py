import streamlit as st
import pandas as pd
import numpy as np
from catboost import CatBoostRegressor
from sklearn.metrics.pairwise import cosine_similarity
import pickle

st.set_page_config(page_title="Оценка стоимости квартир", layout="wide")

st.title("🏠 Анализ рынка недвижимости Москвы")

# Загрузка модели и данных
@st.cache_resource
def load_model():
    model = CatBoostRegressor()
    model.load_model("catboost_final.cbm")
    return model

@st.cache_data
def load_data():
    df = pd.read_csv("realty_clean.csv", encoding='utf-8-sig', sep=';', on_bad_lines='skip', low_memory=False)
    return df

@st.cache_data
def load_embeddings():
    try:
        with open("embeddings.pkl", "rb") as f:
            embeddings_df = pickle.load(f)
        return embeddings_df
    except:
        return None

model = load_model()
df = load_data()
embeddings_df = load_embeddings()

ROOMS_MAP = {"1": 1, "2": 2, "3": 3, "4": 4, "студия": 0}

def predict_price(total_area, area_living, floor, total_floors, build_year, ceiling_height, rooms):
    """Задача 1: Оценка справедливой стоимости"""
    living_ratio = area_living / total_area
    floor_pct = floor / total_floors
    house_age = 2026 - build_year
    total_area_sq = total_area ** 2 / 1000
    
    rooms_encoded = ROOMS_MAP.get(rooms, 0)
    
    base_values = [
        float(total_area), float(floor), float(total_floors), float(build_year),
        float(ceiling_height), 10.0, float(living_ratio), float(floor_pct),
        float(house_age), float(total_area_sq), rooms_encoded, 0, 0, 0, 1, 1
    ]
    
    tfidf_count = len(model.feature_names_) - 16
    base_values.extend([0.0] * tfidf_count)
    
    input_df = pd.DataFrame([base_values], columns=model.feature_names_)
    pred_log = model.predict(input_df)
    return np.expm1(pred_log)[0]

def find_anomalies(threshold=0.2):
    results = []
    for idx, row in df.head(200).iterrows():
        try:
            predicted = predict_price(
                row['total_area'], 
                row.get('area_living', row['total_area'] * 0.7),
                row['floor'], row['total_floors'], row['build_year'],
                row.get('ceiling_height', 2.7),
                str(row.get('rooms', '3'))
            )
            
            deviation = (row['price'] - predicted) / predicted
            
            results.append({
                'Цена': row['price'],
                'Оценка': predicted,
                'Отклонение': f"{deviation*100:.1f}%",
                'Тип': ' НЕДООЦЕНЕНА' if deviation < -threshold else ' ПЕРЕОЦЕНЕНА' if deviation > threshold else ' НОРМА',
                'Адрес': str(row.get('address', ''))[:50]
            })
        except:
            continue
    
    return pd.DataFrame(results).sort_values('Цена', ascending=False)

def find_similar(offer_id, top_k=5):

    if embeddings_df is None:
        return None
    
    offer_id = str(offer_id)
    if offer_id not in embeddings_df['offer_id'].astype(str).values:
        return None
    
    query_idx = embeddings_df[embeddings_df['offer_id'].astype(str) == offer_id].index[0]
    all_embeddings = np.vstack(embeddings_df['clip_embedding'].values)
    similarities = cosine_similarity([all_embeddings[query_idx]], all_embeddings)[0]
    similar_indices = np.argsort(similarities)[::-1][1:top_k+1]
    
    results = []
    for idx in similar_indices:
        offer = embeddings_df.iloc[idx]
        apt_data = df[df['offer_id'].astype(str) == str(offer['offer_id'])].iloc[0]
        results.append({
            'Цена': apt_data['price'],
            'Площадь': apt_data['total_area'],
            'Комнат': apt_data.get('rooms', '?'),
            'Сходство': f"{similarities[idx]*100:.1f}%"
        })
    return pd.DataFrame(results)

tab1, tab2, tab3 = st.tabs(["💰 Оценка стоимости", "🔍 Аномалии", "🔎 Похожие квартиры"])

with tab1:
    st.header("💰 Оценка справедливой стоимости")
    
    col1, col2 = st.columns(2)
    with col1:
        total_area = st.number_input("Общая площадь (м²)", 20.0, 300.0, 75.0)
        floor = st.number_input("Этаж", 1, 100, 8)
        build_year = st.number_input("Год постройки", 1900, 2026, 2022)
    with col2:
        area_living = st.number_input("Жилая площадь (м²)", 15.0, 250.0, 52.0)
        total_floors = st.number_input("Всего этажей", 1, 100, 25)
        ceiling_height = st.number_input("Высота потолков (м)", 2.0, 5.0, 2.8)
    
    rooms = st.selectbox("Количество комнат", list(ROOMS_MAP.keys()))
    
    if st.button("💰 Рассчитать стоимость", type="primary"):
        price = predict_price(total_area, area_living, floor, total_floors, build_year, ceiling_height, rooms)
        
        c1, c2, c3 = st.columns(3)
        c1.metric(" Прогноз цены", f"{price:,.0f} ₽")
        c2.metric(" -20%", f"{price*0.8:,.0f} ₽")
        c3.metric(" +20%", f"{price*1.2:,.0f} ₽")
        
        st.success(f" Справедливая цена: {price*0.8:,.0f} - {price*1.2:,.0f} ₽")

with tab2:
    st.header("🔍 Поиск переоцененных и недооцененных квартир")
    st.caption("Анализ показывает отклонение рыночной цены от предсказанной моделью")
    
    if st.button(" Найти аномалии"):
        with st.spinner("Анализируем рынок..."):
            anomalies_df = find_anomalies(threshold=0.2)
            
            col1, col2 = st.columns(2)
            with col1:
                st.subheader(" НЕДООЦЕНЕННЫЕ")
                undervalued = anomalies_df[anomalies_df['Тип'] == ' НЕДООЦЕНЕНА'].head(10)
                if len(undervalued) > 0:
                    st.dataframe(undervalued, use_container_width=True)
                else:
                    st.info("Нет сильно недооцененных квартир")
            
            with col2:
                st.subheader(" ПЕРЕОЦЕНЕННЫЕ")
                overvalued = anomalies_df[anomalies_df['Тип'] == ' ПЕРЕОЦЕНЕНА'].head(10)
                if len(overvalued) > 0:
                    st.dataframe(overvalued, use_container_width=True)
                else:
                    st.info("Нет сильно переоцененных квартир")

with tab3:
    st.header(" Поиск похожих квартир")
    st.caption("Находит визуально похожие квартиры по фотографиям (CLIP эмбеддинги)")
    
    if embeddings_df is not None:
        sample_ids = df['offer_id'].astype(str).head(10).tolist()
        st.info(f"Примеры ID квартир: {', '.join(sample_ids[:5])}...")
        
        offer_id = st.text_input("Введите offer_id")
        
        if st.button("🔎 Найти похожие"):
            with st.spinner("Ищем похожие квартиры..."):
                similar_df = find_similar(offer_id, top_k=5)
                if similar_df is not None:
                    st.dataframe(similar_df, use_container_width=True)
                else:
                    st.error("Объявление не найдено или эмбеддинги не загружены")
    else:
        st.warning(" Эмбеддинги не загружены. Функция поиска недоступна")

st.markdown("---")
st.caption(" CatBoost + NLP + CLIP | R² = 0.80, MAPE = 19.3%")