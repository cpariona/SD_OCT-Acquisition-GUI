function figura = raster_enface_medicion(archivo)
%RASTER_ENFACE_MEDICION Reconstruye un en face OCT y mide distancias en mm.
%   RASTER_ENFACE_MEDICION abre un selector de archivos OCTOCE .bin.
%   RASTER_ENFACE_MEDICION(ARCHIVO) abre directamente el archivo indicado.
%
%   La ventana muestra un B-scan central para elegir Z inicio y Z fin. Pulse
%   sobre el B-scan para centrar la ventana o ajuste ambos controles. Luego
%   pulse "Reconstruir en face". El boton "Medir 2
%   puntos" permite seleccionar dos posiciones en el en face y calcula dx,
%   dy y la distancia euclidiana en mm usando X/Y del header.
%
%   El payload se mapea en memoria: no se cargan varios GB en RAM. Se aplica
%   remocion DC por B-scan, inversion del detector, linealizacion k por los
%   extremos lambda del header, ventana Hann y el promedio de magnitud OCT
%   entre ambos extremos de profundidad (inclusive).
%   La profundidad se expresa en bins porque el archivo no contiene una
%   calibracion axial fisica ni compensacion de dispersion.

    if nargin < 1 || isempty(archivo)
        [nombre, carpeta] = uigetfile('*.bin', 'Seleccione raster OCT/OCE');
        if isequal(nombre, 0)
            if nargout > 0, figura = gobjects(0); end
            return;
        end
        archivo = fullfile(carpeta, nombre);
    end
    archivo = char(string(archivo));
    [p, mapa, numeroAlines] = abrir_raster_mapeado(archivo);

    A = double(p.scan.alines);
    B = double(p.scan.bscans);
    M = double(p.scan.m_repetitions);
    P = double(p.file_info.pixeles_por_aline);
    if numeroAlines < A * B * M
        error('OCTOCE:Incompleto', ...
            'El raster esta incompleto: hay %.0f de %.0f A-lines.', numeroAlines, A * B * M);
    end

    lambdaInicio = double(p.hardware.k_start_nm);
    lambdaFin = double(p.hardware.k_end_nm);
    if ~isfinite(lambdaInicio) || ~isfinite(lambdaFin) || ...
            lambdaInicio <= 0 || lambdaFin <= 0 || lambdaInicio == lambdaFin
        error('OCTOCE:KLinearization', ...
            'El header no contiene extremos lambda validos para linealizacion k.');
    end
    nFFT = max(8192, 2^nextpow2(P));
    maxZ = nFFT / 2;
    xMM = linspace(double(p.scan.center_x_mm) - double(p.scan.x_length_mm) / 2, ...
        double(p.scan.center_x_mm) + double(p.scan.x_length_mm) / 2, A);
    yMM = linspace(double(p.scan.center_y_mm) - double(p.scan.y_length_mm) / 2, ...
        double(p.scan.center_y_mm) + double(p.scan.y_length_mm) / 2, B);

    bCentral = max(1, round(B / 2));
    maxColumnasPreview = 600;
    indicesXPreview = unique(round(linspace(1, A, min(A, maxColumnasPreview))));
    bloqueCentral = leer_bscan(mapa, bCentral, A, M);
    columnas = indicesXPreview(:) + (0:M-1) * A;
    espectrosPreview = double(bloqueCentral(:, columnas(:)));
    octPreview = reconstruir_bscan(espectrosPreview, P, numel(indicesXPreview), M, ...
        lambdaInicio, lambdaFin, nFFT);
    perfil = mean(octPreview(1:min(2048, maxZ), :), 2, 'omitnan');
    [~, zInicial] = max(perfil(max(2, round(0.002 * maxZ)):end));
    zInicial = zInicial + max(2, round(0.002 * maxZ)) - 1;
    zInicioInicial = max(1, zInicial - 10);
    zFinInicial = min(maxZ, zInicial + 10);

    figura = figure('Name', ['Raster en face · ' nombre_archivo(archivo)], ...
        'NumberTitle', 'off', 'Color', [0.96 0.97 0.99], ...
        'Units', 'normalized', 'Position', [0.04 0.07 0.92 0.84], ...
        'CloseRequestFcn', @cerrarFigura);
    ejeBscan = axes('Parent', figura, 'Units', 'normalized', ...
        'Position', [0.06 0.25 0.39 0.68]);
    imagenBscan = imagesc(ejeBscan, xMM(indicesXPreview), 1:maxZ, octPreview);
    set(ejeBscan, 'YDir', 'reverse', 'Color', 'k');
    colormap(ejeBscan, gray(256));
    ajustar_clim(ejeBscan, octPreview);
    xlabel(ejeBscan, 'Posicion lateral (mm)');
    ylabel(ejeBscan, 'Profundidad (Z bin)');
    title(ejeBscan, sprintf('B-scan central %d/%d · pulse para centrar rango Z', bCentral, B));
    set(imagenBscan, 'ButtonDownFcn', @elegirZDesdeBscan, 'HitTest', 'on');
    hold(ejeBscan, 'on');
    lineaZInicio = yline(ejeBscan, zInicioInicial, 'c-', 'LineWidth', 1.5, 'HitTest', 'off');
    lineaZFin = yline(ejeBscan, zFinInicial, 'm-', 'LineWidth', 1.5, 'HitTest', 'off');
    hold(ejeBscan, 'off');

    ejeEnface = axes('Parent', figura, 'Units', 'normalized', ...
        'Position', [0.53 0.25 0.41 0.68]);
    axis(ejeEnface, 'image');
    grid(ejeEnface, 'on');
    xlabel(ejeEnface, 'X (mm)'); ylabel(ejeEnface, 'Y (mm)');
    title(ejeEnface, 'Seleccione profundidad y reconstruya');
    text(ejeEnface, 0.5, 0.5, 'En face aun no reconstruido', ...
        'Units', 'normalized', 'HorizontalAlignment', 'center', ...
        'Color', [0.35 0.4 0.5]);

    uicontrol(figura, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.06 0.186 0.075 0.034], 'String', 'Z inicio:', ...
        'BackgroundColor', get(figura, 'Color'), 'FontWeight', 'bold');
    editorZInicio = uicontrol(figura, 'Style', 'edit', 'Units', 'normalized', ...
        'Position', [0.135 0.188 0.055 0.038], 'String', num2str(zInicioInicial), ...
        'Callback', @cambiarRangoDesdeEditor);
    pasoMenor = 1 / max(1, maxZ - 1);
    sliderZInicio = uicontrol(figura, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [0.195 0.19 0.255 0.034], 'Min', 1, 'Max', maxZ, ...
        'Value', zInicioInicial, 'SliderStep', [pasoMenor min(1, 50 * pasoMenor)], ...
        'Callback', @cambiarRangoDesdeSlider);
    uicontrol(figura, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.06 0.142 0.075 0.034], 'String', 'Z fin:', ...
        'BackgroundColor', get(figura, 'Color'), 'FontWeight', 'bold');
    editorZFin = uicontrol(figura, 'Style', 'edit', 'Units', 'normalized', ...
        'Position', [0.135 0.144 0.055 0.038], 'String', num2str(zFinInicial), ...
        'Callback', @cambiarRangoDesdeEditor);
    sliderZFin = uicontrol(figura, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [0.195 0.146 0.255 0.034], 'Min', 1, 'Max', maxZ, ...
        'Value', zFinInicial, 'SliderStep', [pasoMenor min(1, 50 * pasoMenor)], ...
        'Callback', @cambiarRangoDesdeSlider);
    botonReconstruir = uicontrol(figura, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.53 0.168 0.16 0.055], 'String', 'Reconstruir en face', ...
        'FontWeight', 'bold', 'Callback', @reconstruirEnface);
    botonMedir = uicontrol(figura, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.705 0.168 0.115 0.055], 'String', 'Medir 2 puntos', ...
        'Enable', 'off', 'Callback', @medirDosPuntos);
    botonLimpiar = uicontrol(figura, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.83 0.168 0.11 0.055], 'String', 'Limpiar medidas', ...
        'Enable', 'off', 'Callback', @limpiarMedidas);
    textoEstado = uicontrol(figura, 'Style', 'text', 'Units', 'normalized', ...
        'Position', [0.06 0.045 0.88 0.085], ...
        'String', sprintf(['%s\nRaster %dx%d · M=%d · %.4g x %.4g mm · ' ...
        'lambda %.4g–%.4g nm · Z sin calibracion axial'], ...
        archivo, A, B, M, double(p.scan.x_length_mm), double(p.scan.y_length_mm), ...
        lambdaInicio, lambdaFin), ...
        'HorizontalAlignment', 'left', 'BackgroundColor', get(figura, 'Color'));

    enfaceActual = [];
    objetosMedida = gobjects(0);
    cancelado = false;

    function fijarRango(inicio, fin)
        inicio = max(1, min(maxZ, round(double(inicio))));
        fin = max(1, min(maxZ, round(double(fin))));
        if inicio > fin, [inicio, fin] = deal(fin, inicio); end
        set(sliderZInicio, 'Value', inicio);
        set(sliderZFin, 'Value', fin);
        set(editorZInicio, 'String', num2str(inicio));
        set(editorZFin, 'String', num2str(fin));
        lineaZInicio.Value = inicio;
        lineaZFin.Value = fin;
        if ~isempty(enfaceActual)
            set(textoEstado, 'String', sprintf( ...
                'Rango Z seleccionado: %d–%d. Pulse "Reconstruir en face" para actualizar.', ...
                inicio, fin));
        end
    end

    function cambiarRangoDesdeSlider(~, ~)
        fijarRango(get(sliderZInicio, 'Value'), get(sliderZFin, 'Value'));
    end

    function cambiarRangoDesdeEditor(~, ~)
        inicio = str2double(get(editorZInicio, 'String'));
        fin = str2double(get(editorZFin, 'String'));
        if ~isfinite(inicio), inicio = get(sliderZInicio, 'Value'); end
        if ~isfinite(fin), fin = get(sliderZFin, 'Value'); end
        fijarRango(inicio, fin);
    end

    function elegirZDesdeBscan(~, ~)
        punto = get(ejeBscan, 'CurrentPoint');
        centro = round(punto(1, 2));
        anchoIzquierdo = round((get(sliderZFin, 'Value') - get(sliderZInicio, 'Value')) / 2);
        fijarRango(centro - anchoIzquierdo, centro + anchoIzquierdo);
    end

    function reconstruirEnface(~, ~)
        zInicio = round(get(sliderZInicio, 'Value'));
        zFin = round(get(sliderZFin, 'Value'));
        rango = zInicio:zFin;
        set([botonReconstruir botonMedir botonLimpiar ...
            sliderZInicio sliderZFin editorZInicio editorZFin], 'Enable', 'off');
        drawnow;
        espera = waitbar(0, sprintf('Promediando Z bins %d–%d...', zInicio, zFin), ...
            'Name', 'Raster en face', 'CreateCancelBtn', @cancelarReconstruccion);
        cancelado = false;
        limpiezaEspera = onCleanup(@() cerrar_waitbar(espera)); %#ok<NASGU>
        try
            usarDFTParcial = numel(rango) <= 32;
            if usarDFTParcial
                pesos = complex(zeros(P, numel(rango)));
                for j = 1:numel(rango)
                    pesos(:, j) = pesos_profundidad(P, lambdaInicio, lambdaFin, nFFT, rango(j));
                end
            end
            imagen = zeros(B, A, 'single');
            for b = 1:B
                bloque = double(leer_bscan(mapa, b, A, M));
                if usarDFTParcial
                    bloque = bloque - mean(bloque, 2);
                    amplitudZ = abs(pesos.' * bloque);
                    amplitud = reshape(mean(amplitudZ, 1), A, M);
                else
                    amplitud = amplitud_rango_fft(bloque, P, A, M, ...
                        lambdaInicio, lambdaFin, nFFT, zInicio, zFin);
                end
                imagen(b, :) = single(mean(amplitud, 2)).';
                if mod(b, max(1, floor(B / 100))) == 0 || b == B
                    if cancelado || ~isgraphics(figura)
                        error('OCTOCE:Cancelado', 'Reconstruccion cancelada por el usuario.');
                    end
                    waitbar(b / B, espera, sprintf('B-scan %d/%d · Z %d–%d', ...
                        b, B, zInicio, zFin));
                    drawnow limitrate;
                end
            end
            enfaceActual = 20 * log10(imagen + 1);
            limpiarMedidas();
            imagesc(ejeEnface, xMM, yMM, enfaceActual);
            set(ejeEnface, 'YDir', 'normal');
            axis(ejeEnface, 'image');
            colormap(ejeEnface, gray(256));
            ajustar_clim(ejeEnface, enfaceActual);
            xlabel(ejeEnface, 'X (mm)'); ylabel(ejeEnface, 'Y (mm)');
            title(ejeEnface, sprintf('En face · promedio Z bins %d–%d', zInicio, zFin));
            set(textoEstado, 'String', sprintf( ...
                'En face listo: promedio de magnitud OCT en Z bins %d–%d. Marque dos puntos.', ...
                zInicio, zFin));
            set([botonMedir botonLimpiar], 'Enable', 'on');
        catch excepcion
            if ~strcmp(excepcion.identifier, 'OCTOCE:Cancelado')
                errordlg(excepcion.message, 'No se pudo reconstruir');
            end
            set(textoEstado, 'String', excepcion.message);
        end
        if isgraphics(figura)
            set([botonReconstruir sliderZInicio sliderZFin editorZInicio editorZFin], ...
                'Enable', 'on');
            if ~isempty(enfaceActual), set([botonMedir botonLimpiar], 'Enable', 'on'); end
        end
    end

    function cancelarReconstruccion(~, ~)
        cancelado = true;
    end

    function medirDosPuntos(~, ~)
        if isempty(enfaceActual), return; end
        figure(figura); axes(ejeEnface); %#ok<LAXES>
        tituloAnterior = get(ejeEnface, 'Title').String;
        title(ejeEnface, 'Seleccione dos puntos');
        [xp, yp] = ginput(2);
        title(ejeEnface, tituloAnterior);
        if numel(xp) ~= 2, return; end
        xp = min(max(xp, min(xMM)), max(xMM));
        yp = min(max(yp, min(yMM)), max(yMM));
        dx = xp(2) - xp(1);
        dy = yp(2) - yp(1);
        distancia = hypot(dx, dy);
        hold(ejeEnface, 'on');
        h1 = plot(ejeEnface, xp, yp, 'c-o', 'LineWidth', 2, ...
            'MarkerFaceColor', 'y', 'MarkerEdgeColor', 'k', 'Tag', 'MedidaEnface');
        h2 = text(ejeEnface, mean(xp), mean(yp), sprintf('  %.4f mm', distancia), ...
            'Color', 'y', 'FontWeight', 'bold', 'BackgroundColor', 'k', ...
            'Margin', 2, 'Tag', 'MedidaEnface');
        hold(ejeEnface, 'off');
        objetosMedida(end+1:end+2) = [h1 h2]; %#ok<AGROW>
        mensaje = sprintf('Distancia = %.6f mm   |   dx = %.6f mm   |   dy = %.6f mm', ...
            distancia, dx, dy);
        set(textoEstado, 'String', mensaje);
        fprintf('%s\n', mensaje);
    end

    function limpiarMedidas(varargin) %#ok<INUSD>
        if ~isempty(objetosMedida)
            delete(objetosMedida(isgraphics(objetosMedida)));
        end
        objetosMedida = gobjects(0);
    end

    function cerrarFigura(~, ~)
        cancelado = true;
        delete(figura);
    end
end


function [p, mapa, N] = abrir_raster_mapeado(archivo)
    [fid, mensaje] = fopen(archivo, 'rb', 'ieee-le');
    if fid < 0, error('OCTOCE:Abrir', 'No se pudo abrir %s: %s', archivo, mensaje); end
    cerrar = onCleanup(@() fclose(fid)); %#ok<NASGU>
    magic = fread(fid, 8, '*uint8')';
    if ~isequal(magic, uint8([double('OCTOCE1'), 0]))
        error('OCTOCE:Formato', 'El archivo no tiene la firma OCTOCE1.');
    end
    versionMayor = fread(fid, 1, '*uint16'); fread(fid, 1, '*uint16');
    flags = fread(fid, 1, '*uint32');
    tipo = fread(fid, 1, '*uint16'); fread(fid, 1, '*uint16');
    longitudJSON = fread(fid, 1, '*uint32');
    crcJSON = fread(fid, 1, '*uint32');
    offset = fread(fid, 1, '*uint64');
    esperadas = fread(fid, 1, '*uint64');
    confirmadas = fread(fid, 1, '*uint64');
    pixeles = fread(fid, 1, '*uint32');
    fread(fid, 1, '*uint32'); fread(fid, 4, '*uint8');
    if isempty(versionMayor) || versionMayor ~= 1 || isempty(tipo) || tipo ~= 1
        error('OCTOCE:Formato', 'Version o tipo de payload no soportado.');
    end
    if isempty(longitudJSON) || isempty(offset) || isempty(pixeles) || ...
            pixeles < 2 || offset < 64 || longitudJSON > offset - 64
        error('OCTOCE:Header', 'Tamano del header o numero de pixeles invalido.');
    end
    bytesJSON = fread(fid, double(longitudJSON), '*uint8');
    if numel(bytesJSON) ~= double(longitudJSON) || crc32_octoce(bytesJSON) ~= crcJSON
        error('OCTOCE:CRC', 'El header JSON esta truncado o tiene CRC32 incorrecto.');
    end
    p = jsondecode(native2unicode(bytesJSON', 'UTF-8'));
    if ~strcmpi(p.scan.pattern, 'raster') || ~strcmpi(p.scan.mode, 'BM')
        error('OCTOCE:RasterBM', 'Esta herramienta requiere un archivo raster en modo BM.');
    end
    fseek(fid, 0, 'eof');
    tamanoArchivo = ftell(fid);
    if tamanoArchivo < double(offset)
        error('OCTOCE:Payload', 'El offset de datos excede el tamano del archivo.');
    end
    disponibles = floor((tamanoArchivo - double(offset)) / (2 * double(pixeles)));
    N = min([double(esperadas), double(confirmadas), disponibles]);
    if bitand(flags, uint32(1)) == 0
        warning('OCTOCE:Incompleto', 'El archivo no esta marcado como completo.');
    end
    p.file_info = struct('archivo', archivo, 'pixeles_por_aline', double(pixeles), ...
        'offset_datos_bytes', double(offset), 'alines_disponibles', N);
    mapa = memmapfile(archivo, 'Offset', double(offset), 'Writable', false, ...
        'Format', {'uint16', [double(pixeles), N], 'raw'}, 'Repeat', 1);
end


function bloque = leer_bscan(mapa, b, A, M)
    primero = (b - 1) * A * M + 1;
    ultimo = primero + A * M - 1;
    bloque = mapa.Data.raw(:, primero:ultimo);
end


function db = reconstruir_bscan(raw, P, cantidadX, M, lambdaInicio, lambdaFin, nFFT)
    raw = raw - mean(raw, 2);
    raw = flipud(raw);
    [orden, i0, i1, alfa] = mapa_k(P, lambdaInicio, lambdaFin);
    ordenado = raw(orden, :);
    remuestreado = (1 - alfa) .* ordenado(i0, :) + alfa .* ordenado(i1, :);
    ventana = 0.5 - 0.5 * cos(2 * pi * (0:P-1)' / max(1, P-1));
    transformada = fft(remuestreado .* ventana, nFFT, 1);
    magnitud = reshape(abs(transformada(2:nFFT/2+1, :)), nFFT/2, cantidadX, M);
    db = 20 * log10(mean(magnitud, 3) + 1);
end


function amplitud = amplitud_rango_fft(raw, P, A, M, lambdaInicio, lambdaFin, ...
        nFFT, zInicio, zFin)
    % For wide windows, one FFT per B-scan is cheaper than many partial DFTs.
    raw = raw - mean(raw, 2);
    raw = flipud(raw);
    [orden, i0, i1, alfa] = mapa_k(P, lambdaInicio, lambdaFin);
    ordenado = raw(orden, :);
    remuestreado = (1 - alfa) .* ordenado(i0, :) + alfa .* ordenado(i1, :);
    ventana = 0.5 - 0.5 * cos(2 * pi * (0:P-1)' / max(1, P-1));
    transformada = fft(remuestreado .* ventana, nFFT, 1);
    % Z bin 1 corresponds to FFT row 2 because the zero-frequency bin is omitted.
    amplitud = reshape(mean(abs(transformada(zInicio+1:zFin+1, :)), 1), A, M);
end


function pesos = pesos_profundidad(P, lambdaInicio, lambdaFin, nFFT, z)
    [orden, i0, i1, alfa] = mapa_k(P, lambdaInicio, lambdaFin);
    ventana = 0.5 - 0.5 * cos(2 * pi * (0:P-1)' / max(1, P-1));
    coeficiente = ventana .* exp(-1i * 2 * pi * z * (0:P-1)' / nFFT);
    pesosOrdenados = accumarray(i0, coeficiente .* (1-alfa), [P 1], @sum, 0) + ...
        accumarray(i1, coeficiente .* alfa, [P 1], @sum, 0);
    pesosInvertidos = complex(zeros(P, 1));
    pesosInvertidos(orden) = pesosOrdenados;
    pesos = flipud(pesosInvertidos);
end


function [orden, i0, i1, alfa] = mapa_k(P, lambdaInicio, lambdaFin)
    lambda = linspace(lambdaInicio, lambdaFin, P)';
    k = 2 * pi ./ lambda;
    [kOrdenado, orden] = sort(k);
    kUniforme = linspace(kOrdenado(1), kOrdenado(end), P)';
    indiceFraccional = interp1(kOrdenado, (1:P)', kUniforme, 'linear');
    i0 = min(P-1, max(1, floor(indiceFraccional)));
    i1 = i0 + 1;
    alfa = (kUniforme - kOrdenado(i0)) ./ (kOrdenado(i1) - kOrdenado(i0));
    alfa = min(1, max(0, alfa));
end


function ajustar_clim(eje, imagen)
    valores = sort(double(imagen(isfinite(imagen))));
    if isempty(valores), clim(eje, [0 1]); return; end
    inferior = valores(max(1, round(0.02 * numel(valores))));
    superior = valores(max(1, round(0.995 * numel(valores))));
    if superior <= inferior, superior = inferior + 1; end
    clim(eje, [inferior superior]);
end


function cerrar_waitbar(espera)
    if isgraphics(espera), delete(espera); end
end


function nombre = nombre_archivo(ruta)
    [~, base, extension] = fileparts(ruta);
    nombre = [base extension];
end


function crc = crc32_octoce(bytes)
    crc = uint32(4294967295);
    polinomio = uint32(hex2dec('EDB88320'));
    for i = 1:numel(bytes)
        crc = bitxor(crc, uint32(bytes(i)));
        for bit = 1:8
            if bitand(crc, uint32(1))
                crc = bitxor(bitshift(crc, -1), polinomio);
            else
                crc = bitshift(crc, -1);
            end
        end
    end
    crc = bitcmp(crc);
end
