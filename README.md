# Blender Nsight CSV Importer

Este script permite trazer para dentro do Blender os dados de modelos 3D que foram exportados como CSV pelo NVIDIA Nsight Graphics.

## Pra que serve

Serve para reconstruir objetos 3D a partir de arquivos de texto (CSV) gerados em capturas de tela de jogos ou aplicações gráficas.

    Importação em Lote: Você pode selecionar vários arquivos de uma vez para importar tudo junto.

    Topologias: Suporta Triangle List, Strip e Fan.

    Dados: Traz as posições dos vértices, normais, cores, texturas (UVs) e até os pesos de animação (Skinning).

## Como usar
Instalação

    Baixe o arquivo .py.

    No Blender, vá em Edit > Preferences > Add-ons > Install.

    Escolha o arquivo e ative o Import-Export: Nsight CSV Importer.

## Importação

    Vá em File > Import > Nsight Graphics CSV (.csv).

    No painel da direita, digite os nomes das colunas conforme aparecem no seu CSV (ex: POSITION0).

    Selecione seus arquivos e clique em Importar.

## Compatibilidade

    Blender 5.0.1: Funcionando perfeitamente nesta versão.

    Outras versões: Não foi testado e pode não funcionar corretamente.

<sub>totalmente gerado por ia</sub>
