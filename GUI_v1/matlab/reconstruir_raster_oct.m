function raster = reconstruir_raster_oct(archivo, varargin)
%RECONSTRUIR_RASTER_OCT Reconstruye un raster BM/MB completo a partir de un
%   archivo .bin OCT/OCE, replicando el pipeline de octoce/processing.py
%   (resta de fondo espectral, inversion, k-linealizacion, ventana de Hann
%   y FFT) para cada B-scan del raster.
%
%   RASTER = RECONSTRUIR_RASTER_OCT(ARCHIVO) lee ARCHIVO con LEER_OCTOCE_BIN
%   y devuelve una estructura RASTER con el volumen en dB, el enface
%   (promedio en profundidad) y los ejes fisicos del escaneo.
%
%   RASTER = RECONSTRUIR_RASTER_OCT(ARCHIVO, 'Nombre', Valor, ...) admite:
%     'FFTSize'         Tamano de FFT con zero-padding (default 8192)
%     'DepthBins'        Bins de profundidad a conservar cerca de DC (default 2048)
%     'DepthStartBin'    Primer bin de profundidad conservado, 1-indexado (default 1)
%     'RemoveDC'         Resta el espectro promedio de cada B-scan (default true)
%     'ReverseSpectrum'  Invierte el eje espectral antes de linealizar (default true)
%     'Linearize'        Remuestrea en k uniforme usando k_start_nm/k_end_nm
%                        del header de hardware (default true)
%
%   Campos de RASTER:
%     .volumen_db   [profundidad x alines x bscans] single, intensidad en dB
%     .enface       [bscans x alines] single, promedio en profundidad
%     .eje_x_mm     posicion lateral de cada A-line (mm)
%     .eje_y_mm     posicion de cada B-scan (mm)
%     .eje_z_bin    indice de bin de profundidad conservado
%     .parametros   header completo (igual a LEER_OCTOCE_BIN)
%     .opciones     opciones de reconstruccion efectivamente usadas
%     .archivo      ruta del archivo fuente
%
%   Ejemplo:
%       raster = reconstruir_raster_oct('data/Alevin_al_aire.bin');
%       imagesc(raster.eje_x_mm, raster.eje_z_bin, raster.volumen_db(:,:,300));
%       colormap(gray); axis image;

    rutaFunciones = fileparts(mfilename('fullpath'));
    if isempty(which('leer_octoce_bin'))
        addpath(rutaFunciones);
    end

    p = inputParser;
    addParameter(p, 'FFTSize', 8192, @(x) isnumeric(x) && isscalar(x) && x > 0);
    addParameter(p, 'DepthBins', 2048, @(x) isnumeric(x) && isscalar(x) && x > 0);
    addParameter(p, 'DepthStartBin', 1, @(x) isnumeric(x) && isscalar(x) && x >= 1);
    addParameter(p, 'RemoveDC', true, @(x) islogical(x) && isscalar(x));
    addParameter(p, 'ReverseSpectrum', true, @(x) islogical(x) && isscalar(x));
    addParameter(p, 'Linearize', true, @(x) islogical(x) && isscalar(x));
    parse(p, varargin{:});
    opciones = p.Results;

    [parametros, alines] = leer_octoce_bin(archivo);

    if ~isfield(parametros, 'scan')
        error('ReconstruirRaster:Header', 'El header no contiene parametros de escaneo (scan).');
    end
    modo = upper(parametros.scan.mode);
    if ~ismember(modo, {'BM', 'MB'})
        error('ReconstruirRaster:Modo', 'Modo de escaneo no soportado: %s', parametros.scan.mode);
    end
    if ~strcmpi(parametros.scan.pattern, 'raster')
        warning('ReconstruirRaster:Patron', ...
            'El patron de escaneo es "%s"; se reconstruira igual asumiendo bscans consecutivos.', ...
            parametros.scan.pattern);
    end
    if isfield(parametros.scan, 'raster_bidirectional') && parametros.scan.raster_bidirectional
        error('ReconstruirRaster:Bidireccional', ...
            ['Este archivo fue adquirido en raster bidireccional. LEER_OCTOCE_BIN ' ...
             'devuelve las A-lines en orden temporal (zigzag), y esta funcion asume ' ...
             'orden espacial consecutivo. Agregue el des-zigzagueo antes de reconstruir.']);
    end

    A = double(parametros.scan.alines);
    B = double(parametros.scan.bscans);
    M = double(parametros.scan.m_repetitions);
    Pix = double(parametros.file_info.pixeles_por_aline);
    Nrequerido = A * B * M;
    if numel(alines) < Nrequerido
        error('ReconstruirRaster:Incompleto', ...
            'El archivo solo tiene %d A-lines confirmadas; se esperaban %d (%d bscans x %d alines x %d repeticiones).', ...
            numel(alines), Nrequerido, B, A, M);
    end

    espectros = cell2mat(alines(1:Nrequerido)); % [Pix x (A*B*M)], uint16
    clear alines

    lambdaInicio = [];
    lambdaFin = [];
    if isfield(parametros, 'hardware') && isfield(parametros.hardware, 'k_start_nm') ...
            && isfield(parametros.hardware, 'k_end_nm')
        lambdaInicio = parametros.hardware.k_start_nm;
        lambdaFin = parametros.hardware.k_end_nm;
    end

    fftSize = opciones.FFTSize;
    binsDisponibles = fftSize / 2; % tras descartar el bin DC de la rfft
    depthIni = max(1, round(opciones.DepthStartBin));
    depthFin = min(depthIni + round(opciones.DepthBins) - 1, binsDisponibles);
    if depthFin < depthIni
        error('ReconstruirRaster:Profundidad', 'Rango de profundidad invalido para FFTSize=%d.', fftSize);
    end
    nDepth = depthFin - depthIni + 1;

    ventana = single(hann(Pix));

    if opciones.Linearize
        if isempty(lambdaInicio) || isempty(lambdaFin)
            error('ReconstruirRaster:Longitud', ...
                'No se hallaron k_start_nm/k_end_nm en parametros.hardware; use ''Linearize'', false.');
        end
        longitudOnda = linspace(lambdaInicio, lambdaFin, Pix)';
        numeroOnda = 2 * pi ./ longitudOnda;
        [numeroOndaOrd, orden] = sort(numeroOnda);
        numeroOndaUniforme = linspace(numeroOndaOrd(1), numeroOndaOrd(end), Pix)';
    end

    volumen = zeros(nDepth, A, B, 'single');

    for b = 1:B
        idx0 = (b - 1) * A * M + 1;
        bloque = espectros(:, idx0:idx0 + A * M - 1); % [Pix x (A*M)] uint16

        if M > 1
            if strcmp(modo, 'BM')
                % Orden local BM: para cada repeticion m, las A alines consecutivas.
                bloque = reshape(bloque, Pix, A, M);
            else
                % Orden local MB: para cada A-line, las M repeticiones consecutivas.
                bloque = reshape(bloque, Pix, M, A);
                bloque = permute(bloque, [1 3 2]);
            end
            trabajo = double(mean(bloque, 3));
        else
            trabajo = double(bloque);
        end

        if opciones.RemoveDC
            % Resta el espectro promedio de las A-lines del B-scan (fondo fijo
            % del espectrometro), igual que reconstruct_oct_complex en Python.
            trabajo = trabajo - mean(trabajo, 2);
        end
        if opciones.ReverseSpectrum
            trabajo = flipud(trabajo);
        end
        if opciones.Linearize
            trabajoOrdenado = trabajo(orden, :);
            trabajo = interp1(numeroOndaOrd, trabajoOrdenado, numeroOndaUniforme, 'linear', 'extrap');
        end
        trabajo = trabajo .* ventana;

        espectro = fft(trabajo, fftSize, 1);
        espectro = espectro(2:fftSize / 2 + 1, :);      % descarta bin DC (equivalente a rfft[1:])
        espectro = espectro(depthIni:depthFin, :);      % recorte de profundidad util

        volumen(:, :, b) = single(20 * log10(abs(espectro) + 1));
    end

    cx = parametros.scan.center_x_mm;
    cy = parametros.scan.center_y_mm;
    ejeX = linspace(cx - parametros.scan.x_length_mm / 2, cx + parametros.scan.x_length_mm / 2, A);
    if B > 1
        ejeY = linspace(cy - parametros.scan.y_length_mm / 2, cy + parametros.scan.y_length_mm / 2, B);
    else
        ejeY = cy;
    end

    raster = struct();
    raster.volumen_db = volumen;
    raster.enface = squeeze(mean(volumen, 1)).'; % [bscans x alines]
    raster.eje_x_mm = ejeX;
    raster.eje_y_mm = ejeY;
    raster.eje_z_bin = (depthIni:depthFin)';
    raster.parametros = parametros;
    raster.opciones = opciones;
    raster.archivo = archivo;
end
