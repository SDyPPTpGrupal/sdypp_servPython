"""Cliente de línea de comandos del contrato v2.2.

Con gRPC ya no alcanza un `curl`: el cliente necesita los stubs. Esto es lo mínimo
para poder probar una réplica a mano y para la demo.

    python3 Clase01/cliente.py localhost:8101 identidad
    python3 Clase01/cliente.py localhost:8101 salud
    python3 Clase01/cliente.py localhost:8101 echo hola
    python3 Clase01/cliente.py localhost:8101 personas
    python3 Clase01/cliente.py localhost:8101 alta "Ada Lovelace" 100200
"""

import os
import sys

import grpc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contrato_pb2 as pb
import contrato_pb2_grpc as pb_grpc

USO = __doc__


def main():
    if len(sys.argv) < 3:
        print(USO)
        return 2

    destino, operacion = sys.argv[1], sys.argv[2].lower()
    argumentos = sys.argv[3:]

    with grpc.insecure_channel(destino) as canal:
        stub = pb_grpc.ServicioStub(canal)
        try:
            if operacion == "identidad":
                respuesta = stub.Identidad(pb.IdentidadPedido(), timeout=5)
            elif operacion == "salud":
                respuesta = stub.Salud(pb.SaludPedido(), timeout=5)
            elif operacion == "echo":
                respuesta = stub.Echo(pb.PingPedido(ping=argumentos[0] if argumentos else ""), timeout=5)
            elif operacion == "personas":
                respuesta = stub.ListarPersonas(pb.ListarPersonasPedido(), timeout=5)
            elif operacion == "alta":
                if len(argumentos) < 2:
                    print("alta necesita nombre y legajo")
                    return 2
                respuesta = stub.CrearPersona(
                    pb.NuevaPersona(nombre=argumentos[0], legajo=int(argumentos[1])), timeout=5
                )
            else:
                print(USO)
                return 2
        except grpc.RpcError as e:
            # El código de estado es parte del contrato: se muestra tal cual para
            # poder compararlo contra lo que devuelve la App Java.
            print(f"{e.code().name}: {e.details()}")
            return 1

    print(respuesta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
