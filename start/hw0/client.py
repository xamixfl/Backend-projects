import socket

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
host = 'localhost'
port = 8080

try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.connect((host, port))
        data = client.recv(1024)

        if data == b"OK\n":
            print("Получены корректные данные от сервера : ОК")
        else:
            print("Данные, полученные от сервера некорректны!")

except ConnectionRefusedError:
    print(f"Не удалось подключиться к серверу {host}:{port}. Убедитесь, что сервер запущен!")
except Exception as e:
    print(f"Произошла непредвиденная сетевая ошибка: {e}")
