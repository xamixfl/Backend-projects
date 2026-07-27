import socket

server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
host = 'localhost'
port = 8080

server.bind((host, port))
server.listen(5)

while True:
    client, address = server.accept()
    client.send(b"OK\n")
    client.close()
    
