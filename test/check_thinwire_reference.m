function check_thinwire_reference(referenceDir, outputFile)
% Execute original public operators on small unambiguous wire fixtures.
addpath(referenceDir);
vertices = [(0:9).' zeros(10,2)];
edges = int32([(1:9).' (2:10).']);
feed = int32([1 2]);
a = AntennaGraph(vertices, int32([1;2]), edges, [], feed);
b = AntennaGraph(vertices, int32([1;2;3]), edges, [], feed);

rng(17, 'twister');
draws = rand(11,1);
rng(17, 'twister');
children = GeneticAlgorithmOperators.crossover(a, b, 0);
masks = false(2,9);
for i = 1:2
    masks(i, double(children(i).ActiveEdgeIdx)) = true;
end

population = WeightedPopulation([a;b;b;a], [3;9;1;1]);
rng(23, 'twister');
selectionDraws = rand(2,1);
rng(23, 'twister');
selected = GeneticAlgorithmOperators.selection(population, 2);
winnerMasks = false(2,9);
for i = 1:2
    candidate = selected.getCandidate(i);
    winnerMasks(i, double(candidate.ActiveEdgeIdx)) = true;
end
value = GainCalculator.computeFitnessFromRadPattern(2, 1/(30+40i), 1, 50);
result = struct('crossoverDraws', draws, 'children', masks, ...
                'selectionDraws', selectionDraws, 'winners', winnerMasks, ...
                'winnerFitness', selected.Weights, 'fitness', value);
fid = fopen(outputFile, 'w');
if fid < 0, error('Cannot open result file'); end
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, '%s', jsonencode(result));
disp('ORIGINAL MATLAB OPERATORS OK');
end
