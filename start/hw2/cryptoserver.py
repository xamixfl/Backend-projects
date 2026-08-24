from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from pydantic import BaseModel
from datetime import datetime, timedelta, timezone

from collections import deque

import httpx

import jwt
import asyncio
import uvicorn
import bcrypt
import os
from dotenv import load_dotenv

from contextlib import asynccontextmanager

load_dotenv()

SECRET_KEY = os.environ["SECRET_KEY"]
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

users_db = {}
crypto_db = {}
coingecko_id_cache = {}         # Словарь для кеширования маппинга тикер -> ID (например, {"BTC": "bitcoin"})
security = HTTPBearer()

# Расписание автоматического обновления

schedule_config = {
    "enabled": True,
    "interval_seconds": 30,
    "last_updated": None,
    "next_update": None
}

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

async def get_coingecko_id(client: httpx.AsyncClient, symbol: str) -> str:
    symbol_lower = symbol.lower()

    if symbol_lower in coingecko_id_cache:
        return coingecko_id_cache[symbol_lower]

    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&symbols={symbol_lower}"
    response = await client.get(url, timeout=10.0)
    
    if response.status_code == 200:
        data = response.json()
        if data and "id" in data[0]:
            coin_id = data[0]["id"]
            coingecko_id_cache[symbol_lower] = coin_id
            return coin_id
            
    return None

async def update_all_prices():
    updated_count = 0
    is_error = False

    symbols = list(crypto_db.keys())
    
    async with httpx.AsyncClient() as client:
        for symbol in symbols:
            if symbol not in crypto_db:
                continue
                
            symbol_lower = symbol.lower()
            
            try:
                response = await fetch_coingecko_market_data(client, symbol_lower)
                
                if response is None or response.status_code != 200:
                    is_error = True
                    continue

                data = response.json()
                if not data:
                    is_error = True
                    continue

                coin_info = data[0]
                current_price = coin_info.get("current_price", 0.0)
                now = datetime.now(timezone.utc).isoformat()
                
            except Exception as e:
                print(f"Ошибка при обновлении {symbol}: {e}")
                is_error = True
                continue

            if symbol in crypto_db:
                crypto_db[symbol]["history"].append({
                    "price": current_price,
                    "timestamp": now 
                })
                crypto_db[symbol]["current_price"] = current_price
                crypto_db[symbol]["last_updated"] = now  
                updated_count += 1

    return updated_count, is_error

async def background_price_updater():
    while True:
        try:
            if schedule_config["enabled"]:
                now = datetime.now(timezone.utc)
                schedule_config["last_updated"] = now.isoformat()
                
                interval = schedule_config["interval_seconds"]
                next_time = now + timedelta(seconds=interval)
                schedule_config["next_update"] = next_time.isoformat()

                updated, _ = await update_all_prices()
                print(f"Обновлено валют: {updated}")
                
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(1)
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Ошибка в воркере: {e}")

# Функция запроса к CoinGecko с обработкой статуса 429 (Too Many Requests)
async def fetch_coingecko_market_data(client: httpx.AsyncClient, symbol_lower: str, max_retries: int = 3):
    url = f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&symbols={symbol_lower}"
    
    for attempt in range(max_retries):
        try:
            response = await client.get(url, timeout=10.0)
            
            if response.status_code == 429:
                wait_time = 2 ** attempt
                await asyncio.sleep(wait_time)
                continue
                
            return response
        except httpx.RequestError:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)
            
    return None
        
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(background_price_updater())
    yield 

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# Аутентификация
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

def create_access_token(username: str):
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {
        "sub": username,  
        "exp": expire     
    }

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    if isinstance(encoded_jwt, bytes):
        encoded_jwt = encoded_jwt.decode('utf-8')
    return encoded_jwt

@app.post("/auth/register")
async def register(user: RegisterRequest):
    if user.username in users_db:
        return JSONResponse(
            status_code=409,
            content={"error": "Имя пользователя уже занято"}
        )
    
    hashed = hash_password(user.password)
    users_db[user.username] = {
        "username": user.username,
        "password": hashed,
    }
    token = create_access_token(user.username)
    return JSONResponse(
        status_code=201,
        content={"token": token}
    )

@app.post("/auth/login")
async def login(user: LoginRequest):
    db_user = users_db.get(user.username)
    if not db_user:
        return JSONResponse(
            status_code=400,
            content={"error": "Пользователь не найден"}
        )

    if not verify_password(user.password, db_user["password"]):
        return JSONResponse(
            status_code=401,
            content={"error": "Неверный пароль!"}
        )
    
    token = create_access_token(user.username)
    return JSONResponse(
        status_code=200,
        content={"token": token}
    )

# CRUD операции для криптовалют

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Недействительный токен")
        return username
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Недействительный токен")
    
@app.get("/crypto")
async def get_crypto(current_user: str = Depends(get_current_user)):
    cryptos = []
    for item in crypto_db.values():
        coin_copy = item.copy()
        if "history" in coin_copy:
            coin_copy["history"] = list(coin_copy["history"])
        cryptos.append(coin_copy)

    return JSONResponse(
        status_code=200,
        content={"cryptos": cryptos}
    )

class CryptoCreateRequest(BaseModel):
    symbol: str

@app.post("/crypto")
async def add_crypto(item: CryptoCreateRequest, current_user: str = Depends(get_current_user)):
    symbol = item.symbol.upper()
    symbol_lower = symbol.lower()

    if symbol in crypto_db:
        return JSONResponse(
            status_code=409,
            content={"error": "Эта криптовалюта уже добавлена для отслеживания"}
        )

    async with httpx.AsyncClient() as client:
        try:
            response = await fetch_coingecko_market_data(client, symbol_lower)
            
            if response is None or response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": "Ошибка при обращении к внешнему API криптовалют (возможно, превышен лимит)"}
                )
            
            data = response.json()
            if not data:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Неизвестный тикер"}
                )
            
            coin_info = data[0]
            current_price = coin_info.get("current_price", 0.0)
            coin_name = coin_info.get("name", symbol)
            now = datetime.now(timezone.utc).isoformat()
            
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Сетевая ошибка: {str(e)}"}
            )
    
    history_deque = deque(maxlen=100)
    history_deque.append({
        "price": current_price,
        "timestamp": now
    })
    
    crypto_db[symbol] = {
        "symbol": symbol,
        "name": coin_name,
        "current_price": current_price,
        "last_updated": now,
        "history": history_deque
    }

    return JSONResponse(
        status_code=201,
        content={
            "crypto": {
                "symbol": symbol,
                "name": coin_name,
                "current_price": current_price,
                "last_updated": now
            }
        }
    )

@app.get("/crypto/{symbol}")
async def get_crypto_by_symbol(symbol: str, current_user: str = Depends(get_current_user)):
    symbol_upper = symbol.upper()
    
    if symbol_upper not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )
        
    crypto_data = crypto_db[symbol_upper].copy()
    crypto_data["history"] = list(crypto_data["history"])
    
    return JSONResponse(
        status_code=200,
        content={"symbol": symbol_upper, "name": crypto_data["name"], "current_price": crypto_data["current_price"], "last_updated": crypto_data["last_updated"]}
    )

@app.put("/crypto/{symbol}/refresh")
async def update_price_by_symbol(symbol: str, current_user: str = Depends(get_current_user)):
    symbol_upper = symbol.upper()
    symbol_lower = symbol.lower()
    if symbol_upper not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )

    async with httpx.AsyncClient() as client:
        try:
            response = await fetch_coingecko_market_data(client, symbol_lower)
            
            if response is None or response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": "Ошибка при обращении к внешнему API криптовалют (возможно, превышен лимит)"}
                )
            
            data = response.json()
            if not data:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Неизвестный тикер"}
                )
            
            coin_info = data[0]
            current_price = coin_info.get("current_price", 0.0)
            now = datetime.now(timezone.utc).isoformat()
            
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Сетевая ошибка: {str(e)}"}
            )

    # Проверяем, не удалили ли валюту, пока мы ждали ответ от API
    if symbol_upper not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта была удалена во время обновления"}
        )

    crypto_db[symbol_upper]["history"].append({
        "price": current_price,
        "timestamp": now
    })
    crypto_db[symbol_upper]["current_price"] = current_price
    crypto_db[symbol_upper]["last_updated"] = now
    crypto_data = crypto_db[symbol_upper].copy()
    crypto_data["history"] = list(crypto_data["history"])

    return JSONResponse(
        status_code=200,
        content={"crypto": crypto_data}
    )

@app.get("/crypto/{symbol}/history")
async def get_history(symbol: str, current_user: str = Depends(get_current_user)):
    symbol= symbol.upper()
    if symbol not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )

    history = list(crypto_db[symbol]["history"])
    return JSONResponse(
        status_code=200,
        content={"symbol": symbol, "history": history}
    )

@app.get("/crypto/{symbol}/stats")
async def get_stats(symbol: str, current_user: str = Depends(get_current_user)):
    symbol = symbol.upper()
    if symbol not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )

    current_price = crypto_db[symbol]["current_price"]
    history = list(crypto_db[symbol]["history"])

    if not history:
        return JSONResponse(
            status_code=200,
            content={
                "symbol": symbol,
                "current_price": current_price,
                "stats": {
                    "min_price": current_price,
                    "max_price": current_price,
                    "avg_price": current_price,
                    "price_change": 0.0,
                    "price_change_percent": 0.0,
                    "records_count": 0
                }
            }
        )

    prices = [item["price"] for item in history]
    
    min_price = min(prices)
    max_price = max(prices)
    records_count = len(prices)
    avg_price = sum(prices) / records_count

    first_price = prices[0]
    price_change = current_price - first_price

    if first_price > 0:
        price_change_percent = (price_change / first_price) * 100
    else:
        price_change_percent = 0.0

    stats = {
        "min_price": min_price, 
        "max_price": max_price, 
        "avg_price": avg_price, 
        "price_change": price_change, 
        "price_change_percent": price_change_percent, 
        "records_count": records_count    
    }

    return JSONResponse(
        status_code=200,
        content={
            "symbol": symbol, 
            "current_price": current_price, 
            "stats": stats
        }
    ) 

@app.delete("/crypto/{symbol}")
async def delete(symbol: str, current_user: str = Depends(get_current_user)):
    symbol_upper = symbol.upper()
    if symbol_upper not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )

    del crypto_db[symbol]
    return JSONResponse(
        status_code=200,
        content={}
    )

# Расписание автоматического обновления

@app.get("/schedule")
async def get_settings(current_user: str = Depends(get_current_user)):
    return JSONResponse(
        status_code=200,
        content=schedule_config
    )

@app.put("/schedule")
async def schedule_change(raw_data: dict, current_user: str = Depends(get_current_user)):
    enabled = raw_data.get("enabled")
    interval_seconds = raw_data.get("interval_seconds")

    if not isinstance(enabled, bool) or not isinstance(interval_seconds, int):
        return JSONResponse(status_code=400, content={"error": "Неверный тип данных"})

    if not (10 <= interval_seconds <= 3600):
        return JSONResponse(status_code=400, content={"error": "Интервал должен быть от 10 до 3600 секунд"})
    
    schedule_config["enabled"] = enabled
    schedule_config["interval_seconds"] = interval_seconds
    
    return JSONResponse(
        status_code=200, 
        content={"enabled": enabled, "interval_seconds": interval_seconds}
    )

@app.post("/schedule/trigger")
async def update_all_handler(current_user: str = Depends(get_current_user)):
    try:
        updated, is_error = await update_all_prices()
        now = datetime.now(timezone.utc).isoformat()

        if is_error and updated == 0:
            return JSONResponse(
                status_code=500, 
                content={"error": "Не удалось обновить ни одну криптовалюту"}
            )
            
        return JSONResponse(
            status_code=200, 
            content={"updated_count": updated, "timestamp": now}
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )

if __name__ == "__main__":
    uvicorn.run("cryptoserver:app", host="0.0.0.0", port=8080)
