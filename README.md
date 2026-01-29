 python COMPS/scrape_clickcompetitions_all.py \
 --category Cash="https://www.clickcompetitions.co.uk/competition-category/cash-competitions/" \
 --category Cars="https://www.clickcompetitions.co.uk/competition-category/car-competitions/" \
 --category Tech="https://www.clickcompetitions.co.uk/competition-category/tech-competitions/" \
 --category Daily="https://www.clickcompetitions.co.uk/competition-category/daily-deals/" \
 --out out/combined_click_comps.csv --out-json out/combined_click_comps.json --html out/index.html


python COMPS/scrape_collectiblecompetitions.py \
  --root https://collectiblecompetitions.co.uk/ \
  --out collectible.csv

python COMPS/scrape_eastcoastraffles_all.py \
  --category AutoDraw="https://eastcoastraffles.co.uk/competition-category/auto-draw/" \
  --category Wednesday="https://eastcoastraffles.co.uk/competition-category/wednesday/" \
  --category Sunday="https://eastcoastraffles.co.uk/competition-category/sunday/" \
  --out out/ecr.csv \
  --out-json out/ecr.json \
  --html out/ecr.html


View the generate page here: https://molinto.github.io/click-competition-odds/