from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from passlib.context import CryptContext  
from datetime import datetime, timedelta, timezone

from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Depends, HTTPException

from collections import deque
from pydantic import BaseModel, Field
import httpx

import jwt
import asyncio

from contextlib import asynccontextmanager

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SECRET_KEY = "super-secret-key"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

users_db = {}
crypto_db = {}
COIN_MAPPING_CACHE = {}
security = HTTPBearer()

# Расписание автоматического обновления

schedule_config = {
    "enabled": True,
    "interval_seconds": 30,
    "last_update": None,
    "next_update": None
}

async def update_all_prices():
    coingecko_id = None
    updated_count = 0
    is_error = False
    async with httpx.AsyncClient() as client:
        for symbol in list(crypto_db.keys()):
            try:
                symbol_upper = symbol.upper()
                coingecko_id = COIN_MAPPING_CACHE.get(symbol_upper)
                if not coingecko_id:
                    is_error = True
                    continue

                url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&include_market_cap=false&include_24hr_vol=false&include_last_updated_at=true"
                response = await client.get(url)
                if response.status_code != 200:
                    is_error = True
                    continue

                data = response.json()
                
                current_price = data[coingecko_id].get("usd", 0.0)
                now = datetime.now(timezone.utc).isoformat()
                
            except Exception as e:
                print(f"Ошибка при обновлении {symbol}: {e}")
                is_error = True
                continue

            crypto_db[symbol]["history"].append({
                        "price": current_price,
                        "timestamp": now })
            crypto_db[symbol]["current_price"] = current_price
            crypto_db[symbol]["last_update"] = now
            updated_count += 1

    return updated_count, is_error

async def background_price_updater():
    while True:
        try:
            if schedule_config["enabled"]:
                now = datetime.now(timezone.utc)
                schedule_config["last_update"] = now.isoformat()
                
                interval = schedule_config["interval_seconds"]
                next_time = now + timedelta(seconds=interval)
                schedule_config["next_update"] = next_time.isoformat()

                updated, _ = await update_all_prices()
                print(f"Обновлено валют: {updated}")
                
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(1)
                
        except Exception as e:
            print(f"Ошибка в фоновом воркере: {e}")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Запуск сервера")

    await load_coin_mapping()
    task = asyncio.create_task(background_price_updater())
    
    yield 

    print("Остановка сервера")
    if task and not task.done():
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
    return encoded_jwt

@app.post("/auth/register")
async def register(user: RegisterRequest):
    if user.username in users_db:
        return JSONResponse(
            status_code=409,
            content={"error": "Имя пользователя уже занято"}
        )
    
    hashed = pwd_context.hash(user.password)
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

    elif not pwd_context.verify(user.password, db_user["password"]):
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
async def load_coin_mapping():
    global COIN_MAPPING_CACHE
    url = "https://api.coingecko.com/api/v3/coins/list"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200:
                coins_data = response.json()
                mapping = {}
                for coin in coins_data:
                    symbol = coin.get("symbol", "").upper()
                    coin_id = coin.get("id")
                    if symbol and coin_id:
                        if symbol not in mapping:
                            mapping[symbol] = coin_id
                
                COIN_MAPPING_CACHE = mapping
                print(f"Успешно загружено и закешировано монет: {len(COIN_MAPPING_CACHE)}")
            else:
                print(f"Ошибка загрузки маппинга CoinGecko: статус {response.status_code}")
        except Exception as e:
            print(f"Сетевая ошибка при загрузке маппинга: {str(e)}")

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
    return JSONResponse(
        status_code=200,
        content={"cryptos": list(crypto_db.values())}
    )

class CryptoCreateRequest(BaseModel):
    symbol: str

@app.post("/crypto")
async def add_crypto(item: CryptoCreateRequest, current_user: str = Depends(get_current_user)):
    symbol = item.symbol.upper()

    if symbol in crypto_db:
        return JSONResponse(
            status_code=409,
            content={"error": "Эта криптовалюта уже добавлена для отслеживания"}
        )

    coingecko_id = COIN_MAPPING_CACHE.get(symbol)
    if not coingecko_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Неизвестный тикер или нет маппинга для CoinGecko"}
        )

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&include_market_cap=false&include_24hr_vol=false&include_last_updated_at=true"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": "Ошибка при обращении к внешнему API криптовалют"}
                )
            data = response.json()
            
            current_price = data[coingecko_id].get("usd", 0.0)
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
        "name": coingecko_id.capitalize(),
        "current_price": current_price,
        "last_updated": now,
        "history": history_deque
    }

    return JSONResponse(
        status_code=201,
        content={
            "crypto": {
                "symbol": symbol,
                "name": crypto_db[symbol]["name"],
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
        content={"crypto": crypto_data}
    )

@app.put("/crypto/{symbol}/refresh")
async def update_price_by_symbol(symbol: str, current_user: str = Depends(get_current_user)):
    symbol_upper = symbol.upper()
    if symbol_upper not in crypto_db:
        return JSONResponse(
            status_code=404,
            content={"error": "Валюта не найдена"}
        )
    
    coingecko_id = COIN_MAPPING_CACHE.get(symbol_upper)
    if not coingecko_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Неизвестный тикер или нет маппинга для CoinGecko"}
        )

    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&include_market_cap=false&include_24hr_vol=false&include_last_updated_at=true"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return JSONResponse(
                    status_code=500,
                    content={"error": "Ошибка при обращении к внешнему API криптовалют"}
                )
            data = response.json()

            if coingecko_id not in data:
                return JSONResponse(
                    status_code=404,
                    content={"error": "Криптовалюта не найдена во внешнем источнике"}
                )
            
            current_price = data[coingecko_id].get("usd", 0.0)
            now = datetime.now(timezone.utc).isoformat()
            
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Сетевая ошибка: {str(e)}"}
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
    symbol= symbol.upper()
    if symbol not in crypto_db:
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

class ScheduleRequest(BaseModel):
    enabled: bool
    interval_seconds: int

@app.put("/schedule")
async def schedule_change(item: ScheduleRequest, current_user: str = Depends(get_current_user)):
    if not (10 <= item.interval_seconds <= 3600):
        return JSONResponse(
            status_code=400,
            content={"error": "Интервал должен быть от 10 до 3600 секунд"}
        )
    
    schedule_config["enabled"] = item.enabled
    schedule_config["interval_seconds"] = item.interval_seconds
    
    return JSONResponse(
        status_code=200, 
        content={
            "enabled": item.enabled, 
            "interval_seconds": item.interval_seconds
        }
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