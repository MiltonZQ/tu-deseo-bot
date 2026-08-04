# Auditoría de cobertura por subtipo

Generado por `scripts/auditar_subtipos.py`. Fuente: WooCommerce (`fetch_all_products`).

`por nombre` es lo que ve `_filtrar_por_subtipo`, que es quien decide en modo estricto.
`por faceta` solo evita ese filtro cuando la faceta es un `atributo` (ver `_subtipo_ya_en_restricciones`).

Productos ofrecibles: 247 — con descripción no vacía: 247 (100%)

| subtipo | por nombre | por faceta | soft | veredicto |
|---|---|---|---|---|
| `realista` | 39 | 27 | False | ok |
| `ventosa` | 12 | 12 | False | ok |
| `vidrio` | 2 | 2 | False | ok |
| `cristal` | 2 | 2 | False | ok |
| `doble` | 28 | 19 | False | ok |
| `textura piel` | 39 | 27 | False | ok |
| `textura` | 39 | 0 | False | ok |
| `piel` | 31 | 0 | False | ok |
| `rabbit` | 5 | 0 | False | ok |
| `punto g` | 24 | 45 | False | ok |
| `clitor` | 39 | 247 | False | ok |
| `clitori` | 39 | 247 | False | ok |
| `hitachi` | 4 | 0 | False | ok |
| `bala` | 15 | 0 | False | ok |
| `huevo vibr` | 6 | 0 | False | ok |
| `prostat` | 3 | 0 | False | ok |
| `próstata` | 3 | 29 | False | ok |
| `cola` | 2 | 29 | False | ok |
| `primera vez` | 0 | 12 | True | ok |
| `vibracion` | 67 | 247 | False | ok |
| `vibración` | 67 | 247 | False | ok |
| `ducha` | 8 | 0 | False | ok |
| `enema` | 8 | 2 | False | ok |
| `base de agua` | 27 | 21 | False | ok |
| `silicona` | 60 | 60 | False | ok |
| `calor` | 2 | 13 | False | ok |
| `frío` | 2 | 2 | False | ok |
| `frio` | 2 | 2 | False | ok |
| `sabores` | 14 | 15 | False | ok |
| `sabor` | 14 | 15 | False | ok |
| `desensibiliz` | 28 | 0 | False | ok |
| `caliente` | 15 | 13 | False | ok |
| `funda` | 12 | 3 | False | ok |
| `fundas` | 5 | 3 | False | ok |
| `funda para el pene` | 3 | 3 | False | ok |
| `arnes` | 12 | 13 | False | ok |
| `arnés` | 12 | 13 | False | ok |
| `liguero` | 5 | 41 | False | ok |
| `pechera` | 2 | 41 | False | ok |
| `encaje` | 7 | 0 | False | ok |
| `body` | 8 | 41 | False | ok |
| `bodies` | 8 | 41 | False | ok |
| `bodys` | 8 | 41 | False | ok |
| `colegiala` | 3 | 0 | False | ok |
| `coneja` | 2 | 0 | False | ok |
| `conejita` | 2 | 0 | False | ok |
| `diabla` | 1 | 0 | False | ok |
| `enfermera` | 1 | 0 | False | ok |
| `mucama` | 2 | 0 | False | ok |
| `playboy` | 1 | 0 | False | ok |
| `policia` | 1 | 0 | False | ok |
| `sailor moon` | 1 | 0 | False | ok |
| `disfraz` | 11 | 41 | False | ok |
| `suspensorio` | 9 | 41 | False | ok |
| `conjunto` | 17 | 41 | False | ok |
| `esposas` | 5 | 8 | False | ok |
| `esposa` | 5 | 0 | False | ok |
| `antifaz` | 2 | 8 | False | ok |
| `antifaces` | 2 | 8 | False | ok |
| `fustas` | 0 | 8 | False | SIN COBERTURA |
| `fusta` | 0 | 8 | False | SIN COBERTURA |
| `latigo` | 1 | 8 | False | ok |
| `látigo` | 1 | 8 | False | ok |
| `amarre` | 2 | 8 | False | ok |
| `amarres` | 1 | 8 | False | ok |
| `mordaza` | 1 | 8 | False | ok |
| `vendas` | 2 | 8 | False | ok |
| `doble estimulacion` | 14 | 19 | False | ok |
| `doble estimulación` | 14 | 19 | False | ok |
| `con app` | 24 | 247 | False | ok |
| `control por app` | 24 | 247 | False | ok |
| `control app` | 24 | 0 | False | ok |
| `app control` | 2 | 247 | False | ok |
| `control remoto` | 22 | 247 | False | ok |
| `recargable` | 8 | 13 | False | ok |
| `inalambrico` | 18 | 247 | False | ok |
| `inalámbrico` | 18 | 247 | False | ok |
| `sencillo` | 4 | 0 | True | ok |
| `simple` | 1 | 0 | True | ok |

## Sin cobertura

- `fustas`
- `fusta`
