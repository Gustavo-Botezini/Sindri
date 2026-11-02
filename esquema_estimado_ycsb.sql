-- Esquema estimado a partir do corpus de logs do Sindri.
-- Gerado por src/schema_estimator.py — cada item traz a evidência
-- que o sustenta. Nada aqui foi lido do catálogo do banco.

-- ----------------------------------------------------------------------
-- usertable: tabela ou view materializada | ~1.053.846 linhas | update 75.945x, insert 63.595x, delete 46.750x
CREATE TABLE public.usertable (
    ycsb_key  integer           NOT NULL,                         -- array, confiança 0.85; ex.: 17, 2, 75
    field1    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: AdN^hPr7t##F!G;^!/
    field2    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: WmD,*&>m &<,AtSX@G
    field3    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: V"c!Sz]"tzGj+A XKs
    field4    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: c~h>&MryWHb[62;IKY
    field5    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: 2Wl_iC!(WsKvX54/>b
    field6    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: YJ441CHOWHX9##!2HO
    field7    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: UZ^OxHilSZ>q[mv_p5
    field8    character varying,                                  -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: 2Hzzyt*]$*bpVwJie.
    field9    text,                                               -- cast, confiança 0.90; valores de 100 a 100 caracteres; ex.:  })w"iruSac'la,S2Z
    field10   character varying                                   -- parametro, confiança 0.70; valores de 100 a 100 caracteres; ex.: ANN?>zHlt9/qL^g_a&
);
ALTER TABLE public.usertable ADD PRIMARY KEY (ycsb_key);  -- índice usertable_pkey
