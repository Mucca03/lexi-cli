# lexi-cli

Letra sincronizada de tu reproductor MPRIS directamente en la terminal.

`lexi.py` consulta el tema actual con `playerctl`, busca la letra en
[LRCLIB](https://lrclib.net/) y resalta la línea que corresponde a la
posición actual del reproductor.

## Requisitos

- Python 3.9 o superior.
- [`playerctl`](https://github.com/altdesktop/playerctl) instalado.
- Un reproductor compatible con MPRIS (Spotify, cmus, mpv, Firefox, etc.).
- Conexión a internet para consultar LRCLIB.

No hace falta instalar dependencias de Python.

## Uso

```bash
chmod +x lexi.py
./lexi.py
```

También puedes ejecutarlo con `python3`:

```bash
python3 lexi.py
```

Opciones:

```bash
./lexi.py --help
./lexi.py --player cmus
./lexi.py --no-color
./lexi.py --no-big-text
```

