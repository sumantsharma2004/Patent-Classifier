import pandas as pd
import os
from openai import AzureOpenAI
import time
from typing import Dict, List
import json
from dotenv import load_dotenv
import re

# Load environment variables from .env file
load_dotenv()

# Azure OpenAI Configuration
AZURE_API_KEY = os.getenv("AZURE_API_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_VERSION = os.getenv("AZURE_API_VERSION", "2025-01-01-preview")
AZURE_MODEL = os.getenv("AZURE_MODEL", "gpt-4o")

# Initialize Azure OpenAI client
client = AzureOpenAI(
    api_key=AZURE_API_KEY,
    api_version=AZURE_API_VERSION,
    azure_endpoint=AZURE_ENDPOINT
)

def consolidate_patent_records(df: pd.DataFrame) -> pd.DataFrame:
    """
    Consolidate multi-row patent records into single rows.
    Handles cases where descriptions are split across multiple rows.
    """
    # Pattern to detect a new publication number (US, CN, KR, EP, WO, JP etc.)
    pub_pattern = re.compile(r'^[A-Z]{2}\d+')

    final_rows = []
    current_record = None

    # Find the publication number and description columns
    pub_col = None
    desc_col = None

    for col in df.columns:
        col_lower = col.strip().lower()
        if 'publication' in col_lower and 'number' in col_lower:
            pub_col = col
        elif 'description' in col_lower:
            desc_col = col

    # If we can't find a publication number column, return as-is
    if not pub_col:
        return df

    for _, row in df.iterrows():
        pub_no = str(row[pub_col]).strip() if pub_col else ""

        # If row begins with a valid publication number – start a new block
        if pub_pattern.match(pub_no):
            # Save previous record
            if current_record:
                final_rows.append(current_record)

            # Start new record - preserve original column names
            current_record = {}
            for col in df.columns:
                if col == pub_col:
                    current_record[col] = pub_no
                elif col == desc_col:
                    current_record[col] = str(row.get(col, ""))
                else:
                    current_record[col] = row.get(col, "")

        else:
            # This is continuation text – append to description field
            if current_record and desc_col:
                extra = str(row.get(desc_col, "")).strip()
                if extra and extra != 'nan':
                    # Check if the previous description already ends with """ (indicating end of description)
                    if not current_record[desc_col].rstrip().endswith('"""'):
                        current_record[desc_col] += "\n" + extra

    # Save last record
    if current_record:
        final_rows.append(current_record)

    # Convert to DataFrame
    if final_rows:
        return pd.DataFrame(final_rows)
    else:
        return df

def create_classification_prompt(publication_number, title, abstract, claims, independent_claims) -> str:
    """Create the prompt for QKD relevance classification"""
    return f"""You are a quantum communication technology expert. Analyze the following patent to determine if it is relevant to a specific QKD (Quantum Key Distribution) last-mile problem system.
 
**TARGET SYSTEM CRITERIA:**
A QKD system for last-mile problem consisting of:
- Entangled single photons transmitted to generate a shared cryptographic key that is immediately available
 
 
**RELEVANT KEYWORDS & SYNONYMS:**
 
1. Quantum Key Distribution (QKD): Quantum cryptographic communication, quantum key exchange, quantum encryption protocol, quantum-secured communication system, quantum information transmission system, quantum-based key establishment, quantum communication framework, quantum secret sharing mechanism
 
2. Device (Alice/Bob): Node, terminal, endpoint, station, transceiver unit, communication module, quantum apparatus, participant unit, sender, receiver
 
3. Optical Fiber: Fiber-optic link, optical waveguide, light-transmission medium, photonic channel, fiber channel, optical transmission line, glass fiber cable, optical conduit
 
4. Transmit Data: Exchange information, communicate signals, send and receive data, convey information, transfer data packets, propagate signals, relay quantum states, communicate bit sequences
 
5. Bidirectionally: In both directions, duplex mode, two-way communication, reciprocal transmission, mutual data exchange, dual-directional signaling, reverse and forward channels, full-duplex mode
 
6. Photons: Quantum light particles, light quanta, optical qubits, single-photon pulses, quantum carriers, photon packets, quantum optical signals, entangled light particles
 
7. Shared Cryptographic Key: Secret key, mutual encryption key, joint generation key, symmetric key, symmetric encryption key, session key, private key, quantum-generated key, secure shared key, common cryptographic token, co-established encryption key, secure key
 
8. Entangled: Correlated, quantum-linked, nonlocally connected, coherently coupled, quantum-correlated, interdependent (quantum state), superposition-coupled, BBM92, E91
**PATENT INFORMATION:**
Publication Number: {publication_number}
Title: {title}
Abstract: {abstract}
Claims: {claims}
Description:{independent_claims}
 
 
**TASK:**
Analyze this patent and respond with ONLY a JSON object in the following format:
{{
   "relevance": "RELEVANT" or "NOT RELEVANT",
   "relevance_percentage": <number between 0-100>,
   "confidence": "HIGH", "MEDIUM", or "LOW",
   "reasoning": "Brief explanation (2-3 sentences) of why this patent is relevant or not relevant",
   "key_features_found": ["list of key QKD features identified in the patent"],
   "protocols_mentioned": ["E91", "BBM92", "B92"] (only include these 3 protocols if explicitly mentioned or clearly described),
   "relevance_source": "CLAIMS" or "DESCRIPTION" or "BOTH" or "ABSTRACT" or "TITLE" (indicate where the key relevant information was found that led to the relevance decision)
}}
 
IMPORTANT CLASSIFICATION RULES:
- Set "relevance" to "RELEVANT" ONLY if relevance_percentage is 75% or higher
- Set "relevance" to "NOT RELEVANT" if relevance_percentage is below 75%
 
The relevance_percentage should be:
- 0-20%: Not relevant at all - no QKD features present
- 21-40%: Minimally relevant - mentions quantum but not QKD specific
- 41-60%: Somewhat relevant - QKD related but not entanglement-based
- 61-74%: Moderately relevant - QKD with some features but missing critical elements
- 75-89%: Highly relevant - QKD with entanglement and most key features
- 90-100%: Extremely relevant - matches all target system criteria including E91/BBM92/B92 protocols
 
Confidence levels should reflect:
- HIGH: Very clear evidence for the score, explicit mentions of key features
- MEDIUM: Good evidence but some ambiguity or missing details
- LOW: Uncertain, limited information, or difficult to assess
 
Respond ONLY with the JSON object, no additional text.
 

"""
def classify_patent_chunked(row: pd.Series, chunk_size: int = 15000) -> Dict:
    """Classify patent using chunking when content is too large"""

    publication_number = row.get('publication number', 'N/A')
    title = row.get('title', 'N/A')
    abstract = row.get('abstract', 'N/A')
    claims = row.get('claims', 'N/A')
    description = str(row.get('description', 'N/A'))

    print(f"  Content too large - using chunked analysis")

    # Strategy: Analyze in parts
    # 1. First chunk: Title + Abstract + Claims (most important)
    # 2. If description is large, split it into chunks

    chunks = []

    # Always include title, abstract, and claims in first chunk
    first_chunk_desc = description[:chunk_size] if len(description) > chunk_size else description
    chunks.append({
        'title': title,
        'abstract': abstract,
        'claims': claims,
        'description': first_chunk_desc,
        'part': 1
    })

    # If description is longer, create additional chunks
    if len(description) > chunk_size:
        remaining_desc = description[chunk_size:]
        part_num = 2
        while remaining_desc:
            chunk_desc = remaining_desc[:chunk_size]
            chunks.append({
                'title': '',
                'abstract': '',
                'claims': '',
                'description': chunk_desc,
                'part': part_num
            })
            remaining_desc = remaining_desc[chunk_size:]
            part_num += 1

    print(f"  Split into {len(chunks)} chunks")

    # Analyze each chunk
    results = []
    for i, chunk in enumerate(chunks):
        try:
            prompt = create_classification_prompt(
                publication_number,
                chunk['title'],
                chunk['abstract'],
                chunk['claims'],
                chunk['description']
            )

            response = client.chat.completions.create(
                model=AZURE_MODEL,
                messages=[
                    {"role": "system", "content": "You are a quantum technology expert specializing in patent analysis."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )

            response_text = response.choices[0].message.content.strip()

            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            result = json.loads(response_text)
            results.append(result)
            print(f"  Chunk {i+1}/{len(chunks)} analyzed: {result.get('relevance_percentage', 0)}%")

        except Exception as e:
            print(f"  Error in chunk {i+1}: {e}")
            continue

    # Combine results - take the highest relevance found
    if not results:
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "LOW",
            "reasoning": "Failed to analyze any chunks",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }

    # Find the result with highest relevance
    best_result = max(results, key=lambda x: x.get('relevance_percentage', 0))

    # Combine key features from all chunks
    all_features = []
    all_protocols = []
    for r in results:
        all_features.extend(r.get('key_features_found', []))
        all_protocols.extend(r.get('protocols_mentioned', []))

    best_result['key_features_found'] = list(set(all_features))
    best_result['protocols_mentioned'] = list(set(all_protocols))
    best_result['reasoning'] = f"[Chunked analysis] {best_result.get('reasoning', '')}"

    return best_result


def classify_patent(row: pd.Series) -> Dict:
    """Send patent data to Azure OpenAI for classification"""

    # Get patent data from row
    publication_number = row.get('publication number', 'N/A')
    title = row.get('title', 'N/A')
    abstract = row.get('abstract', 'N/A')
    claims = row.get('claims', 'N/A')
    independent_claims = row.get('description', 'N/A')

    # Create the prompt with patent data
    prompt = create_classification_prompt(publication_number, title, abstract, claims, independent_claims)

    try:
        response = client.chat.completions.create(
            model=AZURE_MODEL,
            messages=[
                {"role": "system", "content": "You are a quantum technology expert specializing in patent analysis."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            # max_tokens=1000
        )

        # Parse the response
        response_text = response.choices[0].message.content.strip()

        # Try to extract JSON from the response
        # Sometimes the model might add markdown code blocks
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        result = json.loads(response_text)
        return result

    except json.JSONDecodeError as e:
        print(f"Error parsing JSON response: {e}")
        print(f"Response text: {response_text}")
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"Failed to parse response: {response_text[:200]}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }
    except Exception as e:
        error_msg = str(e).lower()
        # Check if it's a context length error
        if 'context' in error_msg or 'token' in error_msg or 'length' in error_msg or 'too large' in error_msg or 'maximum' in error_msg:
            print(f"  Context length exceeded - switching to chunked analysis")
            return classify_patent_chunked(row)

        print(f"Error calling Azure OpenAI: {e}")
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"API error: {str(e)}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }


def main():
    """Main function to process the Excel file"""

    # File path
    excel_file = "SET - 3 - 1 - CHECK 300 2.CSV"

    # Generate output filename based on input filename
    input_basename = os.path.splitext(os.path.basename(excel_file))[0]
    output_file = f"{input_basename}_output.xlsx"
    consolidated_tsv_file = f"{input_basename}_consolidated.tsv"

    print(f"Reading CSV file: {excel_file}")

    # Read the CSV file
    try:
        df = pd.read_csv(excel_file)
        print(f"Successfully loaded {len(df)} rows")
        print(f"Columns found: {df.columns.tolist()}")
    except FileNotFoundError:
        print(f"Error: File '{excel_file}' not found in the current directory.")
        print(f"Current directory: {os.getcwd()}")
        return
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return

    # Normalize column names (lowercase and strip spaces)
    df.columns = df.columns.str.strip().str.lower()

    original_row_count = len(df)

    # Consolidate multi-row patent records
    print(f"\nConsolidating multi-row patent records...")
    df = consolidate_patent_records(df)
    print(f"Consolidated {original_row_count} rows into {len(df)} patent records")

    # Save consolidated data as TSV
    df.to_csv(consolidated_tsv_file, sep='\t', index=False)
    print(f"Consolidated data saved to: {consolidated_tsv_file}")

    # Check if required columns exist
    required_columns = ['publication number', 'title', 'abstract', 'claims']
    missing_columns = [col for col in required_columns if col not in df.columns]

    if missing_columns:
        print(f"Warning: Missing columns: {missing_columns}")
        print(f"Available columns: {df.columns.tolist()}")
        print("Proceeding with available columns...")

    # Process each row
    results = []
    total_rows = len(df)

    print(f"\nProcessing {total_rows} patents...")
    print("=" * 80)

    for idx, row in df.iterrows():
        print(f"\nProcessing row {idx + 1}/{total_rows}")
        print(f"Publication Number: {row.get('publication number', 'N/A')}")

        # Classify the patent
        classification = classify_patent(row)

        # Add classification results to the row - dynamically extract all fields
        result_row = row.to_dict()

        # Iterate through all classification fields and add them to result
        for key, value in classification.items():
            # Convert lists to comma-separated strings for better Excel compatibility
            if isinstance(value, list):
                result_row[key] = ', '.join(str(item) for item in value)
            else:
                result_row[key] = value

        results.append(result_row)

        # Print results with safe key access
        relevance = classification.get('relevance', 'N/A')
        relevance_pct = classification.get('relevance_percentage', 0)
        confidence = classification.get('confidence', 'N/A')
        reasoning = classification.get('reasoning', 'N/A')
        relevance_source = classification.get('relevance_source', 'N/A')

        print(f"Result: {relevance} - {relevance_pct}% (Confidence: {confidence})")
        print(f"Source: {relevance_source}")
        print(f"Reasoning: {str(reasoning)[:100]}...")

        # Add a small delay to avoid rate limiting
        time.sleep(0.5)

    # Create results DataFrame
    results_df = pd.DataFrame(results)

    # Remove unnamed columns (columns that start with 'Unnamed:')
    unnamed_cols = [col for col in results_df.columns if str(col).startswith('Unnamed:')]
    if unnamed_cols:
        print(f"\nRemoving {len(unnamed_cols)} unnamed columns: {unnamed_cols}")
        results_df = results_df.drop(columns=unnamed_cols)

    # Split long descriptions into multiple columns
    # Excel cell limit is ~32,767 characters
    MAX_CELL_LENGTH = 32000

    # Find description column dynamically
    desc_col = None
    for col in results_df.columns:
        if 'description' in str(col).lower():
            desc_col = col
            break

    if desc_col:
        # Check if any descriptions need to be split
        descriptions_to_split = results_df[desc_col].fillna('').astype(str)
        needs_continuation = descriptions_to_split.str.len() > MAX_CELL_LENGTH

        if needs_continuation.any():
            print(f"\nSplitting {needs_continuation.sum()} long descriptions into continuation columns...")

            # Create description_continued column
            desc_continued_col = f'{desc_col}_continued'
            results_df[desc_continued_col] = ''

            for idx in results_df[needs_continuation].index:
                full_desc = str(results_df.loc[idx, desc_col])

                # Split the description
                results_df.loc[idx, desc_col] = full_desc[:MAX_CELL_LENGTH]
                results_df.loc[idx, desc_continued_col] = full_desc[MAX_CELL_LENGTH:]

                # If still too long, handle additional splits
                remaining = full_desc[MAX_CELL_LENGTH:]
                continuation_num = 2
                while len(remaining) > MAX_CELL_LENGTH:
                    col_name = f'{desc_col}_continued_{continuation_num}'
                    if col_name not in results_df.columns:
                        results_df[col_name] = ''

                    results_df.loc[idx, f'{desc_col}_continued_{continuation_num-1}'] = remaining[:MAX_CELL_LENGTH]
                    remaining = remaining[MAX_CELL_LENGTH:]
                    continuation_num += 1

                # Add the final remaining part
                if continuation_num > 2:
                    results_df.loc[idx, f'{desc_col}_continued_{continuation_num-1}'] = remaining

    # Save results to Excel (output_file already defined at the beginning of main())
    results_df.to_excel(output_file, index=False)

    print("\n" + "=" * 80)
    print(f"\nAnalysis complete!")
    print(f"Results saved to: {output_file}")

    # Print summary statistics
    print("\n" + "=" * 80)
    print("SUMMARY STATISTICS:")
    print("=" * 80)

    relevance_counts = results_df['relevance'].value_counts()
    print("\nRelevance Distribution:")
    for category, count in relevance_counts.items():
        percentage = (count / total_rows) * 100
        print(f"  {category}: {count} ({percentage:.1f}%)")

    if 'confidence' in results_df.columns:
        print("\nConfidence Distribution:")
        confidence_counts = results_df['confidence'].value_counts()
        for level, count in confidence_counts.items():
            percentage = (count / total_rows) * 100
            print(f"  {level}: {count} ({percentage:.1f}%)")

    # Print relevant patents
    relevant_patents = results_df[results_df['relevance'] == 'RELEVANT']
    if len(relevant_patents) > 0:
        print(f"\n{len(relevant_patents)} RELEVANT patents found:")

        # Sort by relevance percentage (descending)
        relevant_patents_sorted = relevant_patents.sort_values('relevance_percentage', ascending=False)

        for _, patent in relevant_patents_sorted.iterrows():
            print(f"\n  - {patent.get('publication number', 'N/A')}")
            title = str(patent.get('title', 'N/A'))
            print(f"    Title: {title[:80]}...")
            print(f"    Relevance: {patent.get('relevance_percentage', 0)}%")
            print(f"    Source: {patent.get('relevance_source', 'N/A')}")
            print(f"    Confidence: {patent.get('confidence', 'N/A')}")
            if patent.get('protocols_mentioned'):
                print(f"    Protocols: {patent.get('protocols_mentioned')}")

        # Print average relevance percentage for relevant patents
        avg_relevance = relevant_patents['relevance_percentage'].mean()
        print(f"\n  Average relevance score for RELEVANT patents: {avg_relevance:.1f}%")

        # Show distribution of relevance percentages
        print("\n  Relevance Score Distribution (for RELEVANT patents):")
        print(f"    81-100% (Extremely relevant): {len(relevant_patents[relevant_patents['relevance_percentage'] >= 81])}")
        print(f"    61-80% (Highly relevant): {len(relevant_patents[(relevant_patents['relevance_percentage'] >= 61) & (relevant_patents['relevance_percentage'] < 81)])}")
        print(f"    41-60% (Somewhat relevant): {len(relevant_patents[(relevant_patents['relevance_percentage'] >= 41) & (relevant_patents['relevance_percentage'] < 61)])}")
        print(f"    21-40% (Minimally relevant): {len(relevant_patents[(relevant_patents['relevance_percentage'] >= 21) & (relevant_patents['relevance_percentage'] < 41)])}")
        print(f"    0-20% (Not relevant): {len(relevant_patents[relevant_patents['relevance_percentage'] <= 20])}")

        # Show distribution by relevance source
        if 'relevance_source' in relevant_patents.columns:
            print("\n  Relevance Source Distribution (for RELEVANT patents):")
            source_counts = relevant_patents['relevance_source'].value_counts()
            for source, count in source_counts.items():
                percentage = (count / len(relevant_patents)) * 100
                print(f"    {source}: {count} ({percentage:.1f}%)")

if __name__ == "__main__":
    main()


