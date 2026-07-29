import httpx
import asyncio
import argparse
import sys

async def fetch_url(client, url, timeout):
    try:
        response = await client.get(url, timeout=timeout)
        return {
            'url': url,
            'status_code': response.status_code,
            'headers': response.headers,
            'content': response.text,
            'error': None
        }
    
    except httpx.TimeoutException:
        return {
            'url': url,
            'status_code': None,
            'headers': None,
            'content': None,
            'error': 'Timeout',
            'is_timeout': True
        }
    
    except httpx.RequestError as e:
        return {
            'url': url,
            'status_code': None,
            'headers': None,
            'content': None,
            'error': str(e)
        }
    
def print_result(result):
    print(f"Status: {result['status_code']}")
    for key, value in result['headers'].items():
        formatted_key = "-Indexes-".join([w.capitalize() for w in key.split("-")]) if "-" in key else key.capitalize() # Приведение к нужному форматированию
        print(f"{formatted_key}: {value}")
    print()
    print(result['content'])
    
async def hedged_curl(urls, timeout):
    print(f"\n[DEBUG] Запуск hedged_curl для URL: {urls} с таймаутом {timeout}")  # вот с этой строкой тесты прошли 
    #await asyncio.sleep(0.001)
    async with httpx.AsyncClient() as client:
        tasks = [asyncio.create_task(fetch_url(client, url, timeout)) for url in urls]
        for future in asyncio.as_completed(tasks):
            result = await future
            if result['error'] is not None:
                continue

            print_result(result)

            for task in tasks:
                if not task.done():
                    task.cancel()
            return 0

        print("Все запросы завершились ошибкой!")
        return 228

def main():
    parser = argparse.ArgumentParser(
        description='hedgedcurl - выполняет параллельные запросы к нескольким URL',
        usage='%(prog)s [-t TIMEOUT] url [url ...]'
    )

    parser.add_argument(
        '-t', '--timeout',
        type=int,
        default=15,
        help='таймаут в секундах'
    )

    parser.add_argument('urls', nargs='+', help='Список URL для запросов')

    args = parser.parse_args()

    TIMEOUT = args.timeout
    URLS = args.urls

    exit_code = asyncio.run(hedged_curl(URLS, TIMEOUT))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
