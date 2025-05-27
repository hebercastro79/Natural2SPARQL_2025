from flask import Flask, request, jsonify, send_from_directory
import subprocess
import json
import os
from rdflib import Graph, Namespace
from rdflib.plugins.sparql import prepareQuery
import logging
import sys
import re # Mantido para a checagem final de placeholders

# Configuração do logging do Flask
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
flask_logger = logging.getLogger('flask.app')

app = Flask(__name__, static_folder='src/main/resources/static')

# --- CONFIGURAÇÕES DE CAMINHO DENTRO DO CONTAINER ---
BASE_APP_DIR = "/app"
PLN_PROCESSOR_SCRIPT_PATH = os.path.join(BASE_APP_DIR, "src", "main", "resources", "pln_processor.py")
CWD_FOR_PLN = os.path.join(BASE_APP_DIR, "src", "main", "resources")
SPARQL_TEMPLATES_DIR = os.path.join(BASE_APP_DIR, "src", "main", "resources", "Templates")
ONTOLOGY_FILE_PATH = os.path.join(BASE_APP_DIR, "ontologiaB3.ttl")

# Carregar a ontologia
graph = Graph()
NS_B3 = Namespace("https://dcm.ffclrp.usp.br/lssb/stock-market-ontology#")
NS_STOCK = Namespace("https://dcm.ffclrp.usp.br/lssb/stock-market-ontology#")
NS_RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")
NS_RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
NS_XSD = Namespace("http://www.w3.org/2001/XMLSchema#")
NS_OWL = Namespace("http://www.w3.org/2002/07/owl#")

graph.bind("b3", NS_B3)
graph.bind("stock", NS_STOCK)
graph.bind("rdf", NS_RDF)
graph.bind("rdfs", NS_RDFS)
graph.bind("xsd", NS_XSD)
graph.bind("owl", NS_OWL)

INIT_NS = {
    "b3": NS_B3, "stock": NS_STOCK, "rdf": NS_RDF, "rdfs": NS_RDFS,
    "xsd": NS_XSD, "owl": NS_OWL
}

if os.path.exists(ONTOLOGY_FILE_PATH):
    flask_logger.info(f"Carregando ontologia de: {ONTOLOGY_FILE_PATH}")
    try:
        graph.parse(ONTOLOGY_FILE_PATH, format="turtle")
        flask_logger.info(f"Ontologia carregada com {len(graph)} triplas.")
    except Exception as e:
        flask_logger.error(f"Erro CRÍTICO ao carregar ontologia: {e}. A aplicação pode não funcionar corretamente.", exc_info=True)
else:
    flask_logger.error(f"ARQUIVO DE ONTOLOGIA NÃO ENCONTRADO EM: {ONTOLOGY_FILE_PATH}. As consultas SPARQL falharão.")

@app.route('/', methods=['GET'])
def index():
    flask_logger.info(f"Tentando servir: {app.static_folder} / index2.html")
    try:
        return send_from_directory(app.static_folder, 'index2.html')
    except Exception as e:
        flask_logger.error(f"Erro ao tentar servir o index2.html: {e}", exc_info=True)
        return "Erro ao carregar a interface principal. Verifique os logs do servidor.", 500

@app.route('/processar_pergunta', methods=['POST'])
def processar_pergunta_completa():
    data = request.get_json()
    if not data or 'pergunta' not in data:
        return jsonify({"erro": "Pergunta não fornecida no corpo JSON", "sparqlQuery": "N/A"}), 400

    pergunta_usuario = data['pergunta']
    flask_logger.info(f"Recebida pergunta: '{pergunta_usuario}'")

    pln_output_json_obj = None
    output_str_pln = ""
    try:
        flask_logger.info(f"Chamando PLN: python {PLN_PROCESSOR_SCRIPT_PATH} '{pergunta_usuario}' com CWD: {CWD_FOR_PLN}")
        process_pln = subprocess.run(
            ['python', PLN_PROCESSOR_SCRIPT_PATH, pergunta_usuario],
            capture_output=True, text=True, check=False, cwd=CWD_FOR_PLN, env=dict(os.environ)
        )
        output_str_pln = process_pln.stdout if process_pln.stdout.strip() else process_pln.stderr
        flask_logger.debug(f"Saída bruta PLN (stdout): {process_pln.stdout[:500]}...")
        flask_logger.debug(f"Saída bruta PLN (stderr): {process_pln.stderr[:500]}...")
        flask_logger.info(f"Código de saída do PLN: {process_pln.returncode}")

        if not output_str_pln.strip():
            flask_logger.error("PLN não produziu saída (stdout/stderr).")
            return jsonify({"erro": "PLN não produziu saída.", "sparqlQuery": "N/A (Erro no PLN)"}), 500
        
        pln_output_json_obj = json.loads(output_str_pln)

        if "erro" in pln_output_json_obj:
            flask_logger.error(f"Erro estruturado retornado pelo PLN: {pln_output_json_obj['erro']}")
            return jsonify(pln_output_json_obj), 400
        if "template_nome" not in pln_output_json_obj or "mapeamentos" not in pln_output_json_obj:
            flask_logger.error(f"Saída do PLN inesperada: {pln_output_json_obj}")
            return jsonify({"erro": "Saída do PLN inválida ou incompleta.", "sparqlQuery": "N/A"}), 500
    except json.JSONDecodeError as jde:
        flask_logger.error(f"Erro ao decodificar JSON do PLN: {jde}. Saída PLN: {output_str_pln}")
        # LINHA 101 (APROXIMADAMENTE) CORRIGIDA:
        return jsonify({"erro": "Erro ao decodificar saída do PLN.", "sparqlQuery": "N/A (Erro na decodificação PLN)", "debug_pln_output": output_str_pln}), 500
    except Exception as e_pln:
        flask_logger.error(f"Erro genérico ao executar PLN: {e_pln}", exc_info=True)
        return jsonify({"erro": f"Erro crítico ao executar o processador PLN: {str(e_pln)}", "sparqlQuery": "N/A (Erro no PLN)"}), 500

    template_nome = pln_output_json_obj.get("template_nome")
    mapeamentos = pln_output_json_obj.get("mapeamentos", {})
    flask_logger.info(f"PLN retornou: template='{template_nome}', mapeamentos='{mapeamentos}'")

    sparql_query_string_final = "Consulta SPARQL não pôde ser gerada."
    sparql_query_template_content = "Template SPARQL não carregado."
    try:
        template_filename = f"{template_nome.replace(' ', '_')}.txt"
        template_file_path = os.path.join(SPARQL_TEMPLATES_DIR, template_filename)
        flask_logger.info(f"Tentando carregar template SPARQL de: {template_file_path}")

        if not os.path.exists(template_file_path):
            flask_logger.error(f"Arquivo de template SPARQL não encontrado: {template_file_path}")
            return jsonify({"erro": f"Template SPARQL '{template_filename}' não encontrado.", "sparqlQuery": "N/A"}), 500
        
        with open(template_file_path, 'r', encoding='utf-8') as f_template:
            sparql_query_template_content = f_template.read()
        
        sparql_query_string_final = sparql_query_template_content

        for placeholder_key, valor_raw in mapeamentos.items():
            valor_sparql_formatado = None
            valor_str_raw = str(valor_raw)

            if str(placeholder_key) not in sparql_query_string_final:
                flask_logger.debug(f"Placeholder '{placeholder_key}' do PLN não encontrado no template atual '{template_nome}'. Ignorando.")
                continue

            if placeholder_key == "#DATA#":
                valor_sparql_formatado = f'"{valor_str_raw}"^^xsd:date'
            elif placeholder_key == "#ENTIDADE_NOME#":
                valor_escapado = valor_str_raw.replace('\\', '\\\\').replace('"', '\\"')
                valor_sparql_formatado = f'"{valor_escapado}"'
            elif placeholder_key == "#VALOR_DESEJADO#":
                if ":" not in valor_str_raw and not valor_str_raw.startswith("<"):
                    valor_sparql_formatado = f'b3:{valor_str_raw}'
                else:
                    valor_sparql_formatado = valor_str_raw
            elif placeholder_key == "#SETOR#":
                valor_escapado = valor_str_raw.replace('\\', '\\\\').replace('"', '\\"')
                valor_sparql_formatado = f'"{valor_escapado}"'
            else:
                if str(placeholder_key).startswith("#") and str(placeholder_key).endswith("#"):
                    flask_logger.warning(f"Placeholder '{placeholder_key}' não possui formatação explícita. Tratando como string literal SPARQL.")
                    valor_escapado = valor_str_raw.replace('\\', '\\\\').replace('"', '\\"')
                    valor_sparql_formatado = f'"{valor_escapado}"'
                else:
                    flask_logger.debug(f"Item '{placeholder_key}' em mapeamentos não é um placeholder padrão, ignorando.")
                    continue

            if valor_sparql_formatado is not None:
                flask_logger.info(f"Substituindo '{placeholder_key}' por '{valor_sparql_formatado}' na query.")
                sparql_query_string_final = sparql_query_string_final.replace(str(placeholder_key), valor_sparql_formatado)
        
        flask_logger.info(f"Consulta SPARQL final (após todas as substituições):\n{sparql_query_string_final}")
        
        placeholders_restantes = re.findall(r'(#\w+#)', sparql_query_string_final)
        if placeholders_restantes:
            flask_logger.warning(f"AVISO: Query final AINDA CONTÉM placeholders não substituídos: {', '.join(placeholders_restantes)}. Query:\n{sparql_query_string_final}")

    except Exception as e_template:
        flask_logger.error(f"Erro ao processar template SPARQL: {e_template}", exc_info=True)
        return jsonify({"erro": f"Erro ao gerar consulta SPARQL: {str(e_template)}", "sparqlQuery": sparql_query_template_content}), 500

    resposta_formatada_final = "Não foi possível executar a consulta ou não houve resultados."
    try:
        if not graph or len(graph) == 0:
            flask_logger.error("Ontologia não carregada ou vazia.")
            return jsonify({"erro": "Falha ao carregar a ontologia base.", "sparqlQuery": sparql_query_string_final}), 500
        
        query_obj = prepareQuery(sparql_query_string_final, initNs=INIT_NS)
        flask_logger.info("Executando consulta SPARQL principal...")
        qres = graph.query(query_obj)
        
        resultados_json_list = []
        
        if qres.type == 'SELECT':
            all_rows_from_select = list(qres)
            flask_logger.info(f"--- {template_nome}: Número de linhas/resultados SELECT encontrados: {len(all_rows_from_select)} ---")

            for row_data in all_rows_from_select:
                row_dict = {}
                if hasattr(row_data, 'labels'):
                    for var_name in row_data.labels:
                        value = row_data[var_name]
                        row_dict[str(var_name)] = str(value) if value is not None else None
                else:
                    flask_logger.warning("row_data não tem 'labels', tentando acesso por índice (pode ser impreciso).")
                    for i, val_item in enumerate(row_data):
                         row_dict[f"var{i+1}"] = str(val_item) if val_item is not None else None
                
                if row_dict: # Só adiciona se o dicionário não estiver vazio
                    resultados_json_list.append(row_dict)

            if not resultados_json_list:
                resposta_formatada_final = "Nenhum resultado encontrado."
            else:
                resposta_formatada_final = json.dumps(resultados_json_list)
        
        elif qres.type == 'ASK':
            ask_result = False
            if hasattr(qres, 'askAnswer') and isinstance(qres.askAnswer, list) and len(qres.askAnswer) > 0:
                ask_result = qres.askAnswer[0] 
            elif hasattr(qres, 'askAnswer'):
                ask_result = bool(qres.askAnswer)
            resposta_formatada_final = json.dumps({"resultado_ask": ask_result})
        
        elif qres.type == 'CONSTRUCT' or qres.type == 'DESCRIBE':
            resposta_serializada = qres.serialize(format='turtle')
            if not resposta_serializada.strip():
                resposta_formatada_final = "Nenhum resultado para CONSTRUCT/DESCRIBE."
            else:
                resposta_formatada_final = resposta_serializada
        else:
            resposta_formatada_final = f"Tipo de consulta não suportado: {qres.type}"
            
        flask_logger.info(f"Consulta SPARQL executada. Tipo: {qres.type}. Resposta (início): {str(resposta_formatada_final)[:200]}...")

    except Exception as e_sparql:
        flask_logger.error(f"Erro ao executar consulta SPARQL principal: {e_sparql}", exc_info=True)
        flask_logger.error(f"Consulta que falhou: \n{sparql_query_string_final}")
        return jsonify({"erro": f"Erro ao executar consulta SPARQL: {str(e_sparql)}", "sparqlQuery": sparql_query_string_final}), 500

    return jsonify({
        "sparqlQuery": sparql_query_string_final,
        "resposta": resposta_formatada_final
    })

if __name__ == '__main__':
    local_port = int(os.environ.get("PORT", 5000))
    flask_logger.info(f"Iniciando servidor Flask em http://0.0.0.0:{local_port}")
    app.run(host='0.0.0.0', port=local_port, debug=False)