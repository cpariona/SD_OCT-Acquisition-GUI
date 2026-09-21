function [parametros, alines] = leer_octoce_bin(archivo)
%LEER_OCTOCE_BIN Lee un archivo .bin OCT/OCE v1.0 de la GUI Python.
%   [PARAMETROS, ALINES] = LEER_OCTOCE_BIN(ARCHIVO) devuelve el header JSON
%   junto con datos de integridad y un vector de celdas 1 x N. ALINES{i}
%   es un vector columna uint16 de pixeles espectrales de la i-esima A-line
%   en orden temporal de escaneo. No se aplican DC, k-linearization ni FFT.
%
%   Sin ARCHIVO, abre un selector. Se admiten archivos incompletos: solo se
%   devuelven las A-lines confirmadas por el prefijo y presentes en disco.
%
%   Ejemplo:
%       [p, a] = leer_octoce_bin('adquisicion.bin');
%       disp(p.scan); disp(p.hardware);
%       espectro_primero = a{1};
%       numero_de_alines = numel(a);

    if nargin < 1 || isempty(archivo)
        [nombre, carpeta] = uigetfile('*.bin', 'Seleccione archivo OCT/OCE');
        if isequal(nombre, 0)
            error('OCTOCE:Cancelado', 'No se selecciono un archivo.');
        end
        archivo = fullfile(carpeta, nombre);
    end
    archivo = char(string(archivo));
    [fid, mensaje] = fopen(archivo, 'rb', 'ieee-le');
    if fid < 0
        error('OCTOCE:NoSePuedeAbrir', 'No se pudo abrir %s: %s', archivo, mensaje);
    end
    cerrar = onCleanup(@() fclose(fid)); %#ok<NASGU>

    magic = fread(fid, 8, '*uint8')';
    if ~isequal(magic, uint8([double('OCTOCE1'), 0]))
        error('OCTOCE:Magic', 'El archivo no tiene la firma OCTOCE1.');
    end
    versionMayor = fread(fid, 1, '*uint16');
    versionMenor = fread(fid, 1, '*uint16');
    flags = fread(fid, 1, '*uint32');
    tipoDato = fread(fid, 1, '*uint16');
    fread(fid, 1, '*uint16'); % reservado
    longitudJSON = fread(fid, 1, '*uint32');
    crcJSON = fread(fid, 1, '*uint32');
    offsetDatos = fread(fid, 1, '*uint64');
    alinesEsperadas = fread(fid, 1, '*uint64');
    alinesConfirmadas = fread(fid, 1, '*uint64');
    pixelesPorAline = fread(fid, 1, '*uint32');
    fread(fid, 1, '*uint32'); % reservado
    fread(fid, 4, '*uint8');  % padding del prefijo de 64 bytes

    if isempty(versionMayor) || versionMayor ~= 1 || isempty(versionMenor)
        error('OCTOCE:Version', 'Version de archivo OCT/OCE no soportada.');
    end
    if isempty(tipoDato) || tipoDato ~= 1 || isempty(pixelesPorAline) || pixelesPorAline < 1
        error('OCTOCE:Tipo', 'Tipo de dato o cantidad de pixeles invalidos.');
    end
    if isempty(longitudJSON) || isempty(offsetDatos) || ...
            double(offsetDatos) < 64 || double(longitudJSON) > double(offsetDatos) - 64
        error('OCTOCE:Header', 'Tamano de header invalido.');
    end

    bytesJSON = fread(fid, double(longitudJSON), '*uint8');
    if numel(bytesJSON) ~= double(longitudJSON)
        error('OCTOCE:Header', 'El header JSON esta truncado.');
    end
    if crc32_octoce(bytesJSON) ~= crcJSON
        error('OCTOCE:CRC', 'El CRC32 del header JSON no coincide.');
    end
    parametros = jsondecode(native2unicode(bytesJSON', 'UTF-8'));

    fseek(fid, 0, 'eof');
    bytesArchivo = ftell(fid);
    if bytesArchivo < double(offsetDatos)
        error('OCTOCE:Payload', 'El offset de datos supera el tamano del archivo.');
    end
    disponibles = floor((bytesArchivo - double(offsetDatos)) / (2 * double(pixelesPorAline)));
    N = min([double(alinesConfirmadas), double(alinesEsperadas), disponibles]);
    completo = bitand(flags, uint32(1)) ~= 0 && N == double(alinesEsperadas);
    parametros.file_info = struct( ...
        'archivo', archivo, ...
        'version', sprintf('%d.%d', versionMayor, versionMenor), ...
        'completo', completo, ...
        'alines_esperadas', double(alinesEsperadas), ...
        'alines_confirmadas', N, ...
        'pixeles_por_aline', double(pixelesPorAline), ...
        'offset_datos_bytes', double(offsetDatos), ...
        'orden_devuelto', 'temporal de adquisicion');

    if fseek(fid, double(offsetDatos), 'bof') ~= 0
        error('OCTOCE:Payload', 'No se pudo alcanzar el inicio de los datos.');
    end
    espectros = fread(fid, [double(pixelesPorAline), N], '*uint16');
    if numel(espectros) ~= double(pixelesPorAline) * N
        error('OCTOCE:Payload', 'El payload espectral esta truncado.');
    end

    % En BM, el writer invierte los sweeps adquiridos en sentido contrario
    % para almacenarlos en orden espacial. Deshacerlo devuelve el orden
    % temporal realmente adquirido. MB ya se guarda en orden temporal.
    % La marca linear_bidirectional no existe en archivos lineales antiguos.
    if isfield(parametros, 'scan') && strcmpi(parametros.scan.mode, 'BM')
        rasterBidir = strcmpi(parametros.scan.pattern, 'raster') && ...
            isfield(parametros.scan, 'raster_bidirectional') && ...
            parametros.scan.raster_bidirectional;
        linearBidir = strcmpi(parametros.scan.pattern, 'linear') && ...
            isfield(parametros.scan, 'linear_bidirectional') && ...
            parametros.scan.linear_bidirectional;
        A = double(parametros.scan.alines);
        M = double(parametros.scan.m_repetitions);
        B = double(parametros.scan.bscans);
        if rasterBidir || linearBidir
            for b = 1:B
                for m = 1:M
                    invertir = (rasterBidir && mod(b, 2) == 0) || ...
                        (linearBidir && mod((b - 1) * M + (m - 1), 2) == 1);
                    if invertir
                        primero = (b - 1) * M * A + (m - 1) * A + 1;
                        ultimo = primero + A - 1;
                        if ultimo <= N
                            espectros(:, primero:ultimo) = espectros(:, ultimo:-1:primero);
                        end
                    end
                end
            end
        end
    end

    alines = num2cell(espectros, 1);
end

function crc = crc32_octoce(bytes)
% Mismo CRC32 IEEE que zlib.crc32 en octoce/storage.py, sin Java/toolboxes.
    crc = uint32(4294967295);
    polinomio = uint32(hex2dec('EDB88320'));
    for i = 1:numel(bytes)
        crc = bitxor(crc, uint32(bytes(i)));
        for j = 1:8
            if bitand(crc, uint32(1)) ~= 0
                crc = bitxor(bitshift(crc, -1), polinomio);
            else
                crc = bitshift(crc, -1);
            end
        end
    end
    crc = bitxor(crc, uint32(4294967295));
end
