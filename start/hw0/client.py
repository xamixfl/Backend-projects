import socket

client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
host = 'localhost'
port = 8080

client.connect((host, port))
data = client.recv(1024)

if data == b"OK\n":
    print("Получены корректные данные от сервера : ОК")
else:
    print("Данные, полученные от сервера некорректны!")

client.close()
